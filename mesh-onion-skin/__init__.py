bl_info = {
    "name": "Mesh Onion Skin",
    "author": "HB PARK",
    "version": (2, 3, 2),
    "blender": (5, 0, 0),
    "location": "View3D > Sidebar > Onion Skin",
    "description": "GPU-based onion skin ghosts for 3D mesh animations",
    "category": "Animation",
}

from .mesh_onion_skin import register, unregister
