from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from src.models import Category, DirectLink, ScrapedItem

CATEGORY_LABELS: dict[Category, str] = {
    Category.EMULATION: "1. Émulation Console & Portage PC GTA 6 (PS5 / Xbox / shadPS4)",
    Category.DECOMP: "2. Recompilation Statique, Décompilation & Moteur RAGE 9 / RPF",
    Category.AI_RE: "3. IA appliquée au Reverse Engineering & Traduction Binaire",
    Category.DUMPS: "4. Outils de Dump, Symboles, Shaders & Analyse ELF/RPF",
    Category.GENERAL: "5. Veille Technique GTA 6 Générale",
}


def _render_markdown(
    items: list[ScrapedItem],
    *,
    title: str,
    direct_links_table: list[tuple[DirectLink, str, str, int]] | None = None,
    total_scraped: int = 0,
    total_rejected: int = 0,
    total_stored: int | None = None,
) -> str:
    now = datetime.now(timezone.utc)
    grouped: dict[Category, list[ScrapedItem]] = defaultdict(list)
    for item in items:
        grouped[item.category].append(item)

    lines: list[str] = [
        f"# {title}",
        "",
        f"- **Dernière mise à jour (UTC)** : {now.strftime('%Y-%m-%d %H:%M:%S UTC')}",
        f"- **Résultats affichés** : {len(items)}",
    ]
    if total_stored is not None:
        lines.append(f"- **Total en base SQLite** : {total_stored}")
    if total_scraped > 0:
        lines.append(f"- **Items bruts collectés (session)** : {total_scraped}")
    if total_rejected > 0:
        lines.append(f"- **Items filtrés (spam/bruit/hors-cible)** : {total_rejected}")
    lines.extend(["", "---", ""])

    # Section dédiée : Fichiers & Liens directs détectés
    lines.append("## Fichiers & Liens directs détectés")
    lines.append("")
    if direct_links_table:
        lines.append("| Fichier / Lien direct | Type | Statut HTTP | Taille | Source |")
        lines.append("| --- | --- | --- | --- | --- |")
        for dl, item_title, item_url, item_score in direct_links_table:
            status_str = f"`{dl.http_status} OK`" if dl.is_alive and dl.http_status else (
                f"`{dl.http_status}`" if dl.http_status else "`N/A`"
            )
            size_str = dl.human_size or "—"
            safe_title = item_title.replace("|", "\\|")[:70]
            lines.append(
                f"| [{dl.filename}]({dl.url}) | `{dl.link_type}` | {status_str} | "
                f"{size_str} | [{safe_title}]({item_url}) (`{item_score}/100`) |"
            )
        lines.append("")
    else:
        lines.append("_Aucun lien direct vers fichier/release détecté pour le moment._")
        lines.append("")

    lines.extend(["---", ""])

    if not items:
        lines.append("_Aucun résultat pertinent au-dessus du seuil pour le moment._\n")
        return "\n".join(lines)

    for cat in (Category.DECOMP, Category.EMULATION, Category.DUMPS, Category.AI_RE, Category.GENERAL):
        cat_items = grouped.get(cat, [])
        if not cat_items:
            continue

        lines.append(f"## {CATEGORY_LABELS[cat]}")
        lines.append("")

        for item in cat_items:
            date_str = (
                item.effective_date.astimezone().strftime("%Y-%m-%d %H:%M")
                if item.effective_date
                else "N/A"
            )
            kw_str = ", ".join(f"`{k}`" for k in item.matched_keywords[:8]) or "—"
            meta_parts = [
                f"**Score** : `{item.relevance_score}/100`",
                f"**Source** : [{item.source.value}]({item.url})",
                f"**Auteur** : `{item.author}`",
                f"**Date** : `{date_str}`",
            ]
            if item.playability_status:
                meta_parts.insert(1, f"**Statut** : `{item.playability_status}`")
            if item.score_engagement > 0:
                meta_parts.append(f"**Engagement** : `{item.score_engagement}`")
            if item.github_meta and item.github_meta.primary_language:
                meta_parts.append(f"**Langage** : `{item.github_meta.primary_language}`")

            lines.append(f"### [{item.title}]({item.url})")
            lines.append(" | ".join(meta_parts))
            lines.append(f"- **Lien direct** : [{item.url}]({item.url})")
            lines.append(f"- **Mots-clés** : {kw_str}")
            if item.direct_links:
                dl_links = ", ".join(
                    f"[{dl.display_label}]({dl.url})" + (" ✓" if dl.is_alive else "")
                    for dl in item.direct_links[:5]
                )
                lines.append(f"- **Fichiers / Releases** : {dl_links}")
            if item.summary:
                clean_summary = item.summary.replace("\n", " ").strip()
                if len(clean_summary) > 350:
                    clean_summary = clean_summary[:347] + "..."
                lines.append(f"- **Résumé** : {clean_summary}")
            lines.append("")

    return "\n".join(lines)


def update_daily_markdown_report(
    items: list[ScrapedItem],
    output_dir: str | Path,
    *,
    direct_links_table: list[tuple[DirectLink, str, str, int]] | None = None,
    total_scraped: int = 0,
    total_rejected: int = 0,
    total_stored: int | None = None,
) -> Path:
    """Met à jour en temps réel le rapport quotidien reports/YYYY-MM-DD.md."""
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    filepath = out_dir / f"{today}.md"

    content = _render_markdown(
        items,
        title=f"Veille GTA 6 Recompilation & PC Port — {today}",
        direct_links_table=direct_links_table,
        total_scraped=total_scraped,
        total_rejected=total_rejected,
        total_stored=total_stored,
    )
    tmp_path = filepath.with_suffix(".tmp")
    tmp_path.write_text(content, encoding="utf-8")
    tmp_path.replace(filepath)
    return filepath


def export_markdown_report(
    items: list[ScrapedItem],
    output_dir: str | Path,
    *,
    direct_links_table: list[tuple[DirectLink, str, str, int]] | None = None,
    total_scraped: int = 0,
    total_rejected: int = 0,
) -> Path:
    """Alias vers le rapport quotidien reports/YYYY-MM-DD.md."""
    return update_daily_markdown_report(
        items,
        output_dir,
        direct_links_table=direct_links_table,
        total_scraped=total_scraped,
        total_rejected=total_rejected,
    )
