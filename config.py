# --------------------------------------------------------------------------------

# Copyright (c) 2026 Greenlex Systems Services Incorporated. All rights reserved.

#

# Licensed under the GNU General Public License (GPL).

# Original Architecture & Logic by Greenlex Systems Services Incorporated.

#

# No person or organization is authorized to misrepresent this work or claim

# original authorship for themselves. Proper attribution is mandatory.

# --------------------------------------------------------------------------------

import math
from operator import itemgetter
from typing import List, Tuple, Set, Dict

# --- Versioning and Naming ---

ADDON_VERSION: Tuple[int, int, int] = (0, 1, 0)

# --- Naming Conventions (Mandated LSD Prefix) ---

MOD_PREFIX: str = "LSD_"

WIDGET_PREFIX: str = f"{MOD_PREFIX}Widget_v{ADDON_VERSION[0]}{ADDON_VERSION[1]}"

CUTTER_PREFIX: str = f"{MOD_PREFIX}Cutter_"

BOOL_PREFIX: str = f"{MOD_PREFIX}Bool_"

NATIVE_SPRING_MOD_NAME: str = f"{MOD_PREFIX}NativeSpring"

NATIVE_DAMPER_MOD_NAME: str = f"{MOD_PREFIX}NativeDamper"

NATIVE_SLINKY_MOD_NAME: str = f"{MOD_PREFIX}NativeSlinky"

IK_CONSTRAINT_NAME: str = f"{MOD_PREFIX}IK"

WIDGETS_COLLECTION_NAME: str = f"{MOD_PREFIX}Widgets"

MECHANICAL_PARTS_COLLECTION_NAME: str = "Mechanical_Presets"

COLLISION_COLLECTION_NAME: str = "Physics_Collisions"

# --- Numerical Constants ---

GEAR_BEVEL_TAPER_FACTOR: float = math.cos(math.radians(20))

GIZMO_ROTATION_OFFSET: float = -90.0

WELD_THRESHOLD: float = 0.0001

MIN_BONE_LENGTH: float = 0.01

MIN_GIZMO_SCALE: float = 0.0001

DEFAULT_IK_CHAIN_LENGTH: int = 255

# --- UI Panel Management (LSD Scoped) ---

LSD_PANEL_PROPS: List[str] = [
    "lsd_panel_enabled_dimensions", "lsd_show_panel_dimensions",
    "lsd_panel_enabled_sdf_booleans", "lsd_show_panel_sdf_booleans",
    "lsd_panel_enabled_animation", "lsd_show_panel_animation",
    "lsd_panel_enabled_camera", "lsd_show_panel_camera",
    "lsd_panel_enabled_preferences", "lsd_show_panel_preferences",
]

# --- Mechanical Part Categories and Types ---





















GIZMO_STYLES: List[Tuple[str, str, str]] = [
    ('DEFAULT', "Default (Flat)", "Standard flat 2D gizmos"),
]





BONE_MODES: List[Tuple[str, str, str]] = [
    ('SINGLE', "Group", "Use the global joint tool to edit all selected bones at once"),
    ('INDIVIDUAL', "Individual", "Edit each selected bone's LSD properties individually")
]

BONE_AXES: List[Tuple[str, str, str]] = [
    ('AUTO', "Auto (Z-Align)", "Automatically align bone to local Z axis"),
    ('X', "Local X", "Align bone along local X axis"),
    ('Y', "Local Y", "Align bone along local Y axis"),
    ('Z', "Local Z", "Align bone along local Z axis")
]

# --- Physics Element Data (Density in g/cm³) ---

# Values for gases and liquids represent their solid/frozen state density.

ELEMENT_DATA: Dict[str, Dict[str, float]] = {
    'METALS': {
        'Actinium': 10.07, 'Aluminium': 2.70, 'Americium': 13.67, 'Barium': 3.51,
        'Berkelium': 14.78, 'Beryllium': 1.85, 'Bismuth': 9.78, 'Cadmium': 8.65,
        'Calcium': 1.55, 'Californium': 15.1, 'Cerium': 6.69, 'Cesium': 1.88,
        'Chromium': 7.19, 'Cobalt': 8.90, 'Copper': 8.96, 'Curium': 13.51,
        'Dysprosium': 8.55, 'Einsteinium': 8.84, 'Erbium': 9.07, 'Europium': 5.24,
        'Gadolinium': 7.90, 'Gallium': 5.91, 'Gold': 19.30, 'Hafnium': 13.31,
        'Holmium': 8.80, 'Indium': 7.31, 'Iridium': 22.56, 'Iron': 7.87,
        'Lanthanum': 6.15, 'Lead': 11.34, 'Lithium': 0.53, 'Lutetium': 9.84,
        'Magnesium': 1.74, 'Manganese': 7.47, 'Mercury': 14.18, 'Molybdenum': 10.28,
        'Neodymium': 7.01, 'Neptunium': 20.45, 'Nickel': 8.91, 'Niobium': 8.57,
        'Osmium': 22.59, 'Palladium': 12.02, 'Platinum': 21.45, 'Plutonium': 19.84,
        'Polonium': 9.20, 'Potassium': 0.86, 'Praseodymium': 6.64, 'Promethium': 7.26,
        'Protactinium': 15.37, 'Radium': 5.00, 'Rhenium': 21.02, 'Rhodium': 12.45,
        'Rubidium': 1.53, 'Ruthenium': 12.37, 'Samarium': 7.35, 'Scandium': 2.99,
        'Silver': 10.49, 'Sodium': 0.97, 'Strontium': 2.64, 'Tantalum': 16.65,
        'Technetium': 11.50, 'Terbium': 8.22, 'Thallium': 11.85, 'Thorium': 11.72,
        'Thulium': 9.32, 'Tin': 7.29, 'Titanium': 4.51, 'Tungsten': 19.25,
        'Uranium': 19.10, 'Vanadium': 6.11, 'Ytterbium': 6.97, 'Yttrium': 4.47,
        'Zinc': 7.14, 'Zirconium': 6.52
    },
    'NONMETALS': {
        'Argon': 1.62, 'Bromine': 3.12, 'Carbon': 2.26, 'Chlorine': 2.03,
        'Fluorine': 1.70, 'Helium': 0.19, 'Hydrogen': 0.09, 'Krypton': 2.82,
        'Neon': 1.44, 'Nitrogen': 1.03, 'Oxygen': 1.43, 'Phosphorus': 1.82,
        'Radon': 4.40, 'Selenium': 4.82, 'Sulfur': 2.07, 'Xenon': 3.64
    },
    'SEMIMETALS': {
        'Antimony': 6.69, 'Arsenic': 5.73, 'Astatine': 7.00, 'Boron': 2.46,
        'Germanium': 5.32, 'Silicon': 2.33, 'Tellurium': 6.24
    }
}
