from __future__ import annotations

import re

from src.models import Category, ScrapedItem

# Règles de détection des rôles (ordonnées selon la chaîne d'assemblage logique)
ROLE_RULES: list[tuple[str, str, re.Pattern[str]]] = [
    # Dedicated open-source GPU / Shader translators, probes & RDNA2 ISA tools
    (
        "Exécution Dump / Ému",
        "6. Shaders / GPU",
        re.compile(
            r"\b(victory-xcl-graft|rdna2-conformance|rdna2-dis|rdna-isa-ref|leonida-framelab|xbox-hle-lab|dxil-spirv|vkd3d-proton|directxshadercompiler)\b",
            re.IGNORECASE,
        ),
    ),
    # Dedicated open-source Symbols, Native Handlers & Metadata Config repositories
    (
        "PC Natif / Recomp",
        "4. Symboles / Config",
        re.compile(
            r"\b(gta-vi-data-dumps|gta6-stringsdatabase|gta6-modding-sdk)\b",
            re.IGNORECASE,
        ),
    ),
    # Dedicated open-source Xbox XVD / PS5 SELF-ELF Dumpers & Build Inspectors
    (
        "Exécution Dump / Ému",
        "7. Dumper / ELF-SPRX",
        re.compile(
            r"\b(xvdtool|ps5-app-dumper|ps5-self-pager|ps5-self-to-elf|ps5-self-decrypter|xbox-inspector|itemzflow|fself|self\s+decrypt\w*|elf\s+dump\w*|sprx\s+dump\w*|pkg\s+(?:unpacker|extractor)|pfs\s+decrypt\w*|scarlett\s+dump\w*|xbox\s+dump\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PC Natif / Recomp",
        "1. Portage / Exe PC",
        re.compile(
            r"\b(pc\s+port|native\s+pc|playable\s+build|game_scarlett_master|win64|native\s+exe|pc\s+build)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Exécution Dump / Ému",
        "2. Émulateur / Runtime",
        re.compile(
            r"\b(shadps4|kyty|fpps4|ps5\s+emu\w*|ps4\s+emu\w*|xbox\s+series\s+emu\w*|emulator|hle|translation\s+layer|compatibility\s+layer|orbis|prospero|libSce|neonbridge|flamingo|victory-fork|victory-belobug|victory-7900xtx|marvisak/victory|dexnom/victory|vitoriabr|zettrox|ztrx|belobug|avatarmanz|scarlettport|d3d12_xs|xgameruntime|xcrdapi|scarlett\s+(?:emu\w*|runtime|compat\w*|layer|loader)|xcl|tuxbox)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PC Natif / Recomp",
        "3. Recompilateur Statique",
        re.compile(
            r"\b(static\s+recompil\w*|recompiler|gta6recomp|gtavirecomp|xenonrecomp|n64recomp|unleashedrecomp|elf2cpp|llvm\s+recomp|binary\s+lifting)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PC Natif / Recomp",
        "4. Symboles / Config",
        re.compile(
            r"\b(symbols\.toml|recomp_config|\.toml\b|symbol\s+dump|function\s+symbols|pdb|debug\s+symbols|offsets|native\s+handlers?|strings\s*database|hash\s+lookup)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "PC Natif / Recomp",
        "8. Décompilateur / IA RE",
        re.compile(
            r"\b(llm4decompile|ai\s+decompil\w*|neural\s+decompil\w*|binary\s+translation|ghidra|ida\s+pro|radare2|binary\s+ninja|decompiler|re\s+toolkit|rage9-re|rageforge|rpf9-research|0x52504639|format\s+research)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Commun (RPF / Outils)",
        "5. Outil RPF / RAGE 9",
        re.compile(
            r"\b(rpf\d?|rage\s*9|rage9|rage-tools-vi|sixng|percystar|rpf9-explorer|bcpack|ttpf|gta6-explorer|openvi|forge9|openiv|codewalker|archive\s+extract\w*|rpf\s+parser|asset\s+extract\w*)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Exécution Dump / Ému",
        "6. Shaders / GPU",
        re.compile(
            r"\b(shader\w*|xshader|spir-v|dxil|rdna2|gfx10\.?3|gnm|gnmx|vulkan\s+render\w*|dx12\s+(?:shader\w*|render\w*)|directx\s*12|d3d12_xs)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "Exécution Dump / Ému",
        "7. Dumper / ELF-SPRX",
        re.compile(
            r"\b(dumper|memory\s+dump|eboot\.bin|xvd|xvds|xvc)\b",
            re.IGNORECASE,
        ),
    ),
]

# Règles de détection des dépendances techniques
DEPENDENCY_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("Dump jeu requis", re.compile(r"\b(shadps4|kyty|emulator|recomp\w*|elf2cpp|eboot\.bin|fself|rpf\d?|game_scarlett_master|neonbridge|d3d12_xs|xgameruntime|flamingo|vitoriabr)\b", re.IGNORECASE)),
    ("Vulkan", re.compile(r"\b(vulkan|spir-v|moltenvk)\b", re.IGNORECASE)),
    ("DX12 / DXIL", re.compile(r"\b(dx12|directx\s*12|dxil|hlsl|d3d12_xs|d3d12)\b", re.IGNORECASE)),
    ("LLVM", re.compile(r"\b(llvm|clang)\b", re.IGNORECASE)),
    ("C++ / CMake", re.compile(r"\b(c\+\+|cmake|cmakelists\.txt|\.cpp\b)\b", re.IGNORECASE)),
    ("Rust / Cargo", re.compile(r"\b(rust|cargo\.toml)\b", re.IGNORECASE)),
    ("Python", re.compile(r"\b(python|\.py\b)\b", re.IGNORECASE)),
    ("C# / .NET", re.compile(r"\b(c#|dotnet|\.net|codewalker)\b", re.IGNORECASE)),
    ("PS5 / Orbis (ELF/SPRX)", re.compile(r"\b(ps5|orbis|prospero|sprx|libSce|eboot\.bin|gnm|gnmx)\b", re.IGNORECASE)),
    ("Xbox Scarlett", re.compile(r"\b(scarlett|xbox\s+series|game_scarlett_master|d3d12_xs|xgameruntime|xcrdapi|neonbridge|flamingo|vitoriabr|xvd)\b", re.IGNORECASE)),
    ("RPF / RAGE 9", re.compile(r"\b(rpf\d?|rage\s*9|rage9)\b", re.IGNORECASE)),
    ("symbols.toml", re.compile(r"\b(symbols\.toml|recomp_config)\b", re.IGNORECASE)),
    ("Ghidra / IDA", re.compile(r"\b(ghidra|ida\s+pro|binary\s+ninja|radare2)\b", re.IGNORECASE)),
]


def classify_role_and_deps(item: ScrapedItem) -> tuple[str, str, list[str]]:
    """Détermine (voie, rôle, liste_dépendances) pour organiser la collecte."""
    text = item.searchable_text()

    track = "Commun (RPF / Outils)"
    role = "9. Veille / Discussion"

    for rule_track, rule_role, pattern in ROLE_RULES:
        if pattern.search(text):
            track = rule_track
            role = rule_role
            break
    else:
        if item.category == Category.EMULATION:
            track, role = "Exécution Dump / Ému", "2. Émulateur / Runtime"
        elif item.category == Category.DECOMP:
            track, role = "PC Natif / Recomp", "3. Recompilateur Statique"
        elif item.category == Category.DUMPS:
            track, role = "Exécution Dump / Ému", "7. Dumper / ELF-SPRX"
        elif item.category == Category.AI_RE:
            track, role = "PC Natif / Recomp", "8. Décompilateur / IA RE"

    deps: list[str] = []
    for dep_label, dep_pattern in DEPENDENCY_RULES:
        if dep_pattern.search(text):
            deps.append(dep_label)

    # Si aucune dépendance explicite mais langage connu
    if not deps and item.github_meta and item.github_meta.primary_language:
        deps.append(item.github_meta.primary_language)

    return track, role, deps[:6]
