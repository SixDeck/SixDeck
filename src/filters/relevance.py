from __future__ import annotations

import math
import re
from datetime import datetime, timezone

from src.config import AppConfig
from src.models import Category, GitHubMetadata, ScrapedItem, SourceType

DOC_ONLY_NAMES = {
    "readme",
    "readme.md",
    "readme.txt",
    "readme.rst",
    "license",
    "license.md",
    "license.txt",
    "contributing.md",
    "code_of_conduct.md",
    "security.md",
    ".gitignore",
    ".gitattributes",
    ".github",
}


def _compile_term_pattern(term: str) -> re.Pattern[str]:
    """Compile un motif insensible à la casse avec bornes de mots adaptées."""
    cleaned = term.strip()
    escaped = re.escape(cleaned)
    prefix = r"\b" if cleaned[0].isalnum() else ""
    suffix = r"\b" if cleaned[-1].isalnum() else ""
    return re.compile(f"{prefix}{escaped}{suffix}", re.IGNORECASE)


class RelevanceFilter:
    """Filtre anti-spam/malware, ciblage strict GTA 6 et scoring (0 à 100)."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.blacklist_patterns: list[tuple[str, re.Pattern[str]]] = [
            (term, _compile_term_pattern(term))
            for term in config.blacklist.terms
            if term.strip()
        ]
        self.suspicious_exts = {
            ext.lower() for ext in config.blacklist.suspicious_extensions
        }
        self.trusted_archive_domains = {
            d.lower() for d in config.blacklist.trusted_archive_domains
        }
        self.valid_languages = {
            lang.lower() for lang in config.github_legitimacy.valid_languages
        }

        self.gta6_id_patterns: list[tuple[str, re.Pattern[str]]] = [
            (term, _compile_term_pattern(term))
            for term in config.gta6_identifiers
            if term.strip()
        ]
        self.gta6_high_value_patterns: list[tuple[str, re.Pattern[str]]] = [
            (term, _compile_term_pattern(term))
            for term in config.gta6_high_value_terms
            if term.strip()
        ]

        self.pillar_patterns: dict[Category, tuple[int, list[tuple[str, re.Pattern[str]]]]] = {}
        for key, group in config.keywords.items():
            try:
                cat = Category(key)
            except ValueError:
                continue
            compiled = [
                (term, _compile_term_pattern(term))
                for term in group.terms
                if term.strip()
            ]
            self.pillar_patterns[cat] = (group.weight, compiled)

        self.tech_bonus_patterns: list[tuple[str, re.Pattern[str]]] = [
            (term, _compile_term_pattern(term))
            for term in config.technical_bonus_terms
            if term.strip()
        ]

    def evaluate(self, item: ScrapedItem) -> ScrapedItem:
        """Évalue un item : blacklist, légitimité GitHub, ciblage strict GTA 6 et score."""
        text = item.searchable_text()

        # 1. Blacklist stricte (anti-clickbait / anti-malware / faux cracks)
        blacklisted_term = self._check_blacklist(text)
        if blacklisted_term:
            item.rejected = True
            item.rejection_reason = f"Blacklist: terme suspect détecté ('{blacklisted_term}')"
            item.relevance_score = 0
            return item

        # 2. Critères de légitimité GitHub
        if item.github_meta is not None and item.source == SourceType.GITHUB_REPO:
            gh_rejection = self._check_github_legitimacy(item.github_meta)
            if gh_rejection:
                item.rejected = True
                item.rejection_reason = gh_rejection
                item.relevance_score = 0
                return item

        # 3. Détection des identifiants GTA 6 / RAGE 9 / Leonida
        gta6_matches = [
            term for term, pattern in self.gta6_id_patterns if pattern.search(text)
        ]
        if self.config.general.strict_gta6_mode and not gta6_matches:
            item.rejected = True
            item.rejection_reason = "Hors-cible: aucune mention de GTA 6 / RAGE 9 / Leonida"
            item.relevance_score = 0
            return item

        # 4. Calcul des correspondances techniques par pilier
        breakdown: dict[str, int] = {}
        matched_keywords: list[str] = list(gta6_matches)
        pillar_scores: dict[Category, int] = {}

        for cat, (weight, patterns) in self.pillar_patterns.items():
            cat_matches = [term for term, pattern in patterns if pattern.search(text)]
            if cat_matches:
                score_for_cat = min(40, weight + (len(cat_matches) - 1) * 6)
                pillar_scores[cat] = score_for_cat
                matched_keywords.extend(cat_matches)

        # Bonus termes haute valeur (jeu complet, build jouable, patch émulateur GTA 6, binaires)
        high_value_matches = [
            term for term, pattern in self.gta6_high_value_patterns if pattern.search(text)
        ]
        matched_keywords.extend(high_value_matches)

        # Bonus termes techniques secondaires
        tech_matches = [
            term for term, pattern in self.tech_bonus_patterns if pattern.search(text)
        ]
        matched_keywords.extend(tech_matches)

        # En mode strict GTA 6, exiger AU MOINS un terme technique (évite le bruit/hype non technique)
        if self.config.general.strict_gta6_mode and not (
            pillar_scores or high_value_matches or tech_matches
        ):
            item.rejected = True
            item.rejection_reason = (
                "Hors-cible: mention GTA 6 sans contexte technique (port/decomp/emu/RPF)"
            )
            item.relevance_score = 0
            return item

        # Base ciblage GTA 6 (calibré 0-100 cohérent avec PortWatch VI)
        breakdown["gta6_target"] = 8 if gta6_matches else 0

        # Bonus termes haute valeur (max 22)
        if high_value_matches:
            hv_score = min(22, 12 + (len(high_value_matches) - 1) * 4)
        else:
            hv_score = 0
        breakdown["gta6_high_value"] = hv_score

        # Score mots-clés piliers (max 28)
        keyword_score = 0
        if pillar_scores:
            best_cat = max(pillar_scores, key=lambda c: pillar_scores[c])
            item.category = best_cat
            other_pillars_bonus = sum(
                min(5, s // 5) for c, s in pillar_scores.items() if c != best_cat
            )
            keyword_score = min(28, int(round(pillar_scores[best_cat] * 0.72)) + other_pillars_bonus)
        else:
            item.category = Category.GENERAL
        breakdown["keywords"] = keyword_score

        # Contexte technique secondaire (max 8)
        breakdown["technical_context"] = min(8, len(tech_matches) * 2)

        # Fraîcheur (max 10)
        breakdown["recency"] = self._score_recency(item.effective_date)

        # Engagement communautaire (max 12)
        breakdown["engagement"] = self._score_engagement(item)

        # Code source valide (max 10)
        breakdown["valid_code"] = self._score_valid_code(item)

        # Bonus maximal si lien direct vivant (HTTP 200) vers archive/release/config recomp (.toml)
        breakdown["direct_links"] = self._score_direct_links(item)

        # Pondération par type de source & statut de jouabilité (évite qu'un simple post batte un vrai build)
        source_bonus = 0
        has_alive_dl = any(dl.is_alive for dl in item.direct_links)
        if item.extra.get("portwatch_verified"):
            source_bonus += 10
        if item.source == SourceType.GITHUB_RELEASE and (keyword_score > 0 or hv_score > 0):
            source_bonus += 8
        elif item.source == SourceType.GITHUB_REPO:
            source_bonus += 4
        elif item.source == SourceType.ARXIV and (keyword_score > 0 or hv_score > 0):
            source_bonus += 4
        elif item.source in {SourceType.GITHUB_COMMIT, SourceType.GITHUB_PR}:
            source_bonus -= 5
        elif item.source in {SourceType.REDDIT, SourceType.FORUM, SourceType.WEB_DORK} and not has_alive_dl:
            source_bonus -= 10

        st_low = (item.playability_status or "").lower()
        if "playable" in st_low:
            source_bonus += 10
        elif "world" in st_low or "in-game" in st_low:
            source_bonus += 6
        elif "boot" in st_low or "menu" in st_low:
            source_bonus += 4
        breakdown["source_bonus"] = source_bonus

        total = sum(breakdown.values())
        item.relevance_score = max(0, min(100, total))
        item.score_breakdown = breakdown
        item.matched_keywords = list(dict.fromkeys(matched_keywords))

        if item.relevance_score < self.config.general.min_relevance_score:
            item.rejected = True
            item.rejection_reason = (
                f"Score insuffisant ({item.relevance_score} < "
                f"{self.config.general.min_relevance_score})"
            )
        else:
            item.rejected = False
            item.rejection_reason = None

        return item

    def filter_items(
        self, items: list[ScrapedItem]
    ) -> tuple[list[ScrapedItem], list[ScrapedItem]]:
        """Évalue une liste d'items et retourne (acceptés_triés, rejetés)."""
        accepted: list[ScrapedItem] = []
        rejected: list[ScrapedItem] = []

        for item in items:
            evaluated = self.evaluate(item)
            if evaluated.rejected:
                rejected.append(evaluated)
            else:
                accepted.append(evaluated)

        accepted.sort(
            key=lambda x: (
                x.relevance_score,
                x.effective_date.timestamp() if x.effective_date else 0.0,
            ),
            reverse=True,
        )
        return accepted, rejected

    def _check_blacklist(self, text: str) -> str | None:
        for term, pattern in self.blacklist_patterns:
            if pattern.search(text):
                return term
        return None

    def _check_github_legitimacy(self, meta: GitHubMetadata) -> str | None:
        rules = self.config.github_legitimacy
        now = datetime.now(timezone.utc)

        # 1. Compte trop récent (< 30 jours) sans activité préalable
        if meta.owner_created_at is not None:
            owner_dt = (
                meta.owner_created_at
                if meta.owner_created_at.tzinfo is not None
                else meta.owner_created_at.replace(tzinfo=timezone.utc)
            )
            age_days = (now - owner_dt).days
            public_repos = meta.owner_public_repos if meta.owner_public_repos is not None else 0
            followers = meta.owner_followers if meta.owner_followers is not None else 0
            if (
                age_days < rules.min_account_age_days
                and public_repos < rules.min_account_repos_if_new
                and followers == 0
            ):
                return (
                    f"GitHub suspect: compte '{meta.owner_login}' créé il y a {age_days}j "
                    f"({public_repos} dépôts, 0 abonnés)"
                )

        # 2. Fichiers racine suspects (.zip/.exe uniquement ou README-only)
        if meta.root_files:
            lower_files = [f.lower() for f in meta.root_files]
            non_doc_files = [f for f in lower_files if f not in DOC_ONLY_NAMES]

            if not non_doc_files:
                return "GitHub suspect: dépôt sans code (uniquement README/fichiers doc)"

            suspicious_files = [
                f for f in non_doc_files if any(f.endswith(ext) for ext in self.suspicious_exts)
            ]
            if suspicious_files and len(suspicious_files) == len(non_doc_files):
                return (
                    f"GitHub suspect: dépôt contenant uniquement des binaires/archives "
                    f"({', '.join(suspicious_files[:3])})"
                )

        # 2.5. Déduplication des forks sans commit original
        if (
            rules.reject_unchanged_forks
            and meta.is_fork
            and meta.fork_ahead_by is not None
            and meta.fork_ahead_by == 0
        ):
            return "GitHub fork sans commit original (0 commits ahead du dépôt parent)"

        # 3. Vérification de la présence de code source réel + détection faux dépôts IA (ratio README/code)
        if rules.require_valid_code:
            if meta.has_readme_only:
                return "GitHub suspect: dépôt uniquement composé d'un README"

            if meta.inspected:
                if meta.total_code_bytes < rules.min_code_bytes:
                    return (
                        f"GitHub suspect: volume de code insuffisant "
                        f"({meta.total_code_bytes} octets)"
                    )
                if (
                    meta.readme_bytes > 4000
                    and meta.total_code_bytes < 1500
                    and (meta.readme_bytes / max(meta.total_code_bytes, 1))
                    > rules.max_readme_to_code_ratio
                ):
                    return (
                        f"GitHub spam IA suspect: README énorme ({meta.readme_bytes} o) "
                        f"pour code quasi vide ({meta.total_code_bytes} o)"
                    )
                repo_langs = {lang.lower() for lang in meta.languages}
                if repo_langs and not (repo_langs & self.valid_languages):
                    return (
                        f"GitHub hors-cible: aucun langage système/code attendu "
                        f"({', '.join(meta.languages.keys())})"
                    )
            elif meta.primary_language is None:
                return "GitHub suspect: aucun langage de programmation détecté"

        return None

    def _score_recency(self, dt: datetime | None) -> int:
        if dt is None:
            return 0
        now = datetime.now(timezone.utc)
        age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        if age_days <= 2:
            return 10
        if age_days <= 7:
            return 7
        if age_days <= 14:
            return 5
        if age_days <= 30:
            return 3
        return 1

    def _score_engagement(self, item: ScrapedItem) -> int:
        engagement = max(0, item.score_engagement)
        comments = max(0, item.comments_count)

        if item.source in {
            SourceType.GITHUB_REPO,
            SourceType.GITHUB_RELEASE,
            SourceType.GITHUB_COMMIT,
        }:
            if engagement == 0:
                return 0
            return min(12, int(round(math.log10(engagement + 1) * 4.2)))

        if item.source == SourceType.REDDIT:
            combined = engagement + comments * 2
            if combined <= 1:
                return 0
            return min(10, int(round(math.log10(combined + 1) * 3.5)))

        return 0

    def _score_valid_code(self, item: ScrapedItem) -> int:
        if item.github_meta is None:
            return 0

        meta = item.github_meta
        langs = {lang.lower() for lang in meta.languages}
        if meta.primary_language:
            langs.add(meta.primary_language.lower())

        matched_valid = langs & self.valid_languages
        if not matched_valid:
            return 0

        core_systems = {"c", "c++", "rust", "assembly", "zig", "glsl", "hlsl"}
        if matched_valid & core_systems:
            return 10
        return 6

    def _score_direct_links(self, item: ScrapedItem) -> int:
        if not item.direct_links:
            return 0

        alive_recomp_or_release = [
            dl
            for dl in item.direct_links
            if dl.is_alive and dl.link_type in {"recomp_config", "release"}
        ]
        if alive_recomp_or_release:
            return 22

        alive_archives_or_bins = [
            dl
            for dl in item.direct_links
            if dl.is_alive and dl.link_type in {"archive", "binary"}
        ]
        if alive_archives_or_bins:
            return 16

        alive_source = [
            dl for dl in item.direct_links if dl.is_alive and dl.link_type == "source"
        ]
        if alive_source:
            return 10

        alive_any = [dl for dl in item.direct_links if dl.is_alive]
        if alive_any:
            return 8

        # Liens candidats détectés mais HEAD bloqué/non-200
        return 4
