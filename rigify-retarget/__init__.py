bl_info = {
    "name": "Source to Rigify Direct Retarget",
    "author": "GitHub Copilot",
    "description": "One-way Unreal and Mixamo to Rigify retargeting with FK/IK authoring.",
    "version": (0, 4, 0),
    "blender": (3, 6, 0),
    "location": "3D View > Sidebar > Item",
    "warning": "",
    "category": "Animation"
}

from .direct_retarget import register, unregister
