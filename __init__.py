bl_info = {
    "name": "Animation Shortcuts",
    "author": "",
    "version": (1, 0, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Transform Shortcuts",
    "description": "Live Offset tools and quick Transform copy/paste shortcuts for pose bones",
    "category": "Animation",
}

from . import live_transform_offset

def register():
    live_transform_offset.register()

def unregister():
    live_transform_offset.unregister()