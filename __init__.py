bl_info = {
    "name": "Live Transform Offset Propagator",
    "author": "Your Name",
    "version": (2, 1, 0),
    "blender": (4, 5, 0),
    "location": "View3D > Sidebar > Live Offset",
    "description": "Display live transform offsets and apply them to all keyframes",
    "category": "Animation",
}

from . import live_transform_offset

def register():
    live_transform_offset.register()

def unregister():
    live_transform_offset.unregister()