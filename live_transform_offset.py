import bpy
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import FloatProperty, FloatVectorProperty, BoolProperty, StringProperty


# ─────────────────────────────────────────────────────────────────────────────
# Core logic
# ─────────────────────────────────────────────────────────────────────────────

def _iter_channels(bone):
    """Yield (data_path, array_index, live_value) for every transform channel."""
    name = bone.name

    path = f'pose.bones["{name}"].location'
    for i, v in enumerate(bone.location):
        yield path, i, v

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        q = bone.rotation_quaternion
        for i, v in enumerate((q.w, q.x, q.y, q.z)):
            yield path, i, v
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i, v in enumerate(bone.rotation_axis_angle):
            yield path, i, v
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i, v in enumerate(bone.rotation_euler):
            yield path, i, v

    path = f'pose.bones["{name}"].scale'
    for i, v in enumerate(bone.scale):
        yield path, i, v


def _get_action_fcurves(action, obj):
    """Return the FCurves collection driving obj's channels.

    Blender >= 4.4 replaced the flat Action.fcurves layout with layered,
    per-slot actions (Action.layers -> strips -> channelbags), so the
    legacy attribute no longer exists. This resolves the right channelbag
    for the object's assigned action slot, falling back to the legacy
    attribute on older Blender versions.
    """
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves

    anim_data = obj.animation_data
    slot = anim_data.action_slot if anim_data else None
    if slot is None:
        return None

    for layer in action.layers:
        for strip in layer.strips:
            for channelbag in strip.channelbags:
                if channelbag.slot_handle == slot.handle:
                    return channelbag.fcurves
    return None


def collect_offsets(context):
    """Return list of (fcurve, offset, data_path, array_index, bone_name)."""
    obj = context.active_object
    if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
        return []
    if not obj.animation_data or not obj.animation_data.action:
        return []

    action = obj.animation_data.action
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return []

    selected = context.selected_pose_bones or []
    if not selected:
        return []

    current_frame = float(context.scene.frame_current)
    pending = []

    for bone in selected:
        for data_path, array_index, live_val in _iter_channels(bone):
            fc = fcurves.find(data_path, index=array_index)
            if fc is None or fc.lock or fc.mute:
                continue

            offset = live_val - fc.evaluate(current_frame)
            if abs(offset) < 1e-9:
                continue

            pending.append((fc, offset, data_path, array_index, bone.name))

    return pending


def apply_offsets(pending):
    """Apply the collected offsets to all keyframes."""
    for fc, offset, *_ in pending:
        for kp in fc.keyframe_points:
            kp.co.y += offset
            kp.handle_left.y += offset
            kp.handle_right.y += offset
        fc.update()


def get_keyed_value(action, obj, data_path, array_index, frame):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return None
    fc = fcurves.find(data_path, index=array_index)
    if fc is None:
        return None
    return fc.evaluate(frame)


# ─────────────────────────────────────────────────────────────────────────────
# PropertyGroup – stores editable offsets for the active bone
# ─────────────────────────────────────────────────────────────────────────────

def _update_loc(self, context):
    if self.suppress_update:
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        if keyed is not None:
            bone.location[i] = keyed + self.loc_offset[i]
        else:
            bone.location[i] = self.loc_offset[i]


def _update_rot(self, context):
    if self.suppress_update:
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            if keyed is not None:
                bone.rotation_quaternion[i] = keyed + self.rot_offset[i]
            else:
                bone.rotation_quaternion[i] = self.rot_offset[i]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            if keyed is not None:
                bone.rotation_axis_angle[i] = keyed + self.rot_offset[i]
            else:
                bone.rotation_axis_angle[i] = self.rot_offset[i]
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i in range(3):
            keyed = get_keyed_value(action, obj, path, i, frame)
            if keyed is not None:
                bone.rotation_euler[i] = keyed + self.rot_offset[i]
            else:
                bone.rotation_euler[i] = self.rot_offset[i]


def _update_scale(self, context):
    if self.suppress_update:
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        if keyed is not None:
            bone.scale[i] = keyed + self.scale_offset[i]
        else:
            bone.scale[i] = self.scale_offset[i]


class LIVEOFFSET_PG_settings(PropertyGroup):
    suppress_update: BoolProperty(default=False)

    loc_offset: FloatVectorProperty(
        name="Location Offset",
        size=3,
        subtype='TRANSLATION',
        precision=5,
        update=_update_loc,
    )
    rot_offset: FloatVectorProperty(
        name="Rotation Offset",
        size=4,
        precision=5,
        update=_update_rot,
    )
    scale_offset: FloatVectorProperty(
        name="Scale Offset",
        size=3,
        subtype='XYZ',
        precision=5,
        update=_update_scale,
    )


def sync_offsets_from_bone(context):
    """Push current live offsets of the active bone into the PropertyGroup."""
    wm = context.window_manager
    settings = wm.live_offset_settings
    bone = context.active_pose_bone
    obj = context.active_object

    if not bone or not obj or not obj.animation_data or not obj.animation_data.action:
        return

    action = obj.animation_data.action
    frame = float(context.scene.frame_current)
    name = bone.name

    settings.suppress_update = True

    # Location
    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        settings.loc_offset[i] = (bone.location[i] - keyed) if keyed is not None else bone.location[i]

    # Rotation
    if bone.rotation_mode == 'QUATERNION':
        q = bone.rotation_quaternion
        for i, v in enumerate((q.w, q.x, q.y, q.z)):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_quaternion', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
    elif bone.rotation_mode == 'AXIS_ANGLE':
        for i, v in enumerate(bone.rotation_axis_angle):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_axis_angle', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
    else:
        for i, v in enumerate(bone.rotation_euler):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_euler', i, frame)
            settings.rot_offset[i] = (v - keyed) if keyed is not None else v
        settings.rot_offset[3] = 0.0

    # Scale
    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        settings.scale_offset[i] = (bone.scale[i] - keyed) if keyed is not None else bone.scale[i]

    settings.suppress_update = False


# ─────────────────────────────────────────────────────────────────────────────
# Redraw handler – keeps the panel live while dragging manipulators
# ─────────────────────────────────────────────────────────────────────────────

def _depsgraph_update(scene, depsgraph):
    for window in bpy.context.window_manager.windows:
        screen = window.screen
        if not screen:
            continue
        for area in screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# ─────────────────────────────────────────────────────────────────────────────
# Operator
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_OT_apply(Operator):
    """Apply live transform offsets to all keyframes in the active action"""
    bl_idname = "liveoffset.apply"
    bl_label = "Apply Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        pending = collect_offsets(context)

        if not pending:
            self.report({'INFO'}, "Nothing to do – all offsets are zero")
            return {'CANCELLED'}

        bpy.ops.ed.undo_push(message="Live Transform Offset")
        apply_offsets(pending)
        context.scene.frame_set(context.scene.frame_current)

        bones = {p[4] for p in pending}
        self.report(
            {'INFO'},
            f"Shifted {len(pending)} channel(s) across {len(bones)} bone(s)"
        )
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj
            and obj.type == 'ARMATURE'
            and obj.mode == 'POSE'
            and obj.animation_data
            and obj.animation_data.action
            and context.selected_pose_bones
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI Panel
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_PT_panel(Panel):
    bl_label = "Live Offset"
    bl_idname = "LIVEOFFSET_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Live Offset"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        wm = context.window_manager

        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        if obj.mode != 'POSE':
            layout.label(text="Switch to Pose Mode", icon='ERROR')
            return
        if not obj.animation_data or not obj.animation_data.action:
            layout.label(text="No active Action", icon='ERROR')
            return

        selected = context.selected_pose_bones or []
        if not selected:
            layout.label(text="Select one or more bones", icon='INFO')
            return

        # Keep the editable properties in sync with the current live pose.
        # Guarded so a lookup error (e.g. Blender API changes) shows a
        # message instead of leaving the whole panel silently blank.
        try:
            sync_offsets_from_bone(context)
            pending = collect_offsets(context)
        except Exception as exc:
            layout.label(text=f"Live Offset error: {exc}", icon='ERROR')
            return

        settings = wm.live_offset_settings

        # Header
        box = layout.box()
        box.label(text=f"Selected: {len(selected)} bone(s)", icon='BONE_DATA')
        box.label(text=f"Frame: {context.scene.frame_current}", icon='TIME')

        if not pending:
            box.label(text="All offsets ≈ 0", icon='CHECKMARK')
        else:
            box.label(text=f"{len(pending)} channel(s) with offset", icon='MODIFIER')

        # ── Active bone section ──────────────────────────────────────────────
        bone = context.active_pose_bone
        if bone:
            col = layout.column(align=True)
            col.label(text=f"Active: {bone.name}", icon='BONE_DATA')

            # Rotation Mode dropdown (same as Blender's own UI)
            row = col.row()
            row.label(text="Rotation Mode")
            row.prop(bone, "rotation_mode", text="")

            # Location Offset
            box = layout.box()
            box.label(text="Location Offset")
            box.prop(settings, "loc_offset", text="")

            # Rotation Offset (adapts to current mode)
            box = layout.box()
            if bone.rotation_mode == 'QUATERNION':
                box.label(text="Rotation Offset (Quaternion)")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=0, text="W")
                row.prop(settings, "rot_offset", index=1, text="X")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=2, text="Y")
                row.prop(settings, "rot_offset", index=3, text="Z")
            elif bone.rotation_mode == 'AXIS_ANGLE':
                box.label(text="Rotation Offset (Axis-Angle)")
                box.prop(settings, "rot_offset", index=0, text="Angle")
                row = box.row(align=True)
                row.prop(settings, "rot_offset", index=1, text="X")
                row.prop(settings, "rot_offset", index=2, text="Y")
                row.prop(settings, "rot_offset", index=3, text="Z")
            else:
                box.label(text="Rotation Offset (Euler)")
                box.prop(settings, "rot_offset", index=0, text="X")
                box.prop(settings, "rot_offset", index=1, text="Y")
                box.prop(settings, "rot_offset", index=2, text="Z")

            # Scale Offset
            box = layout.box()
            box.label(text="Scale Offset")
            box.prop(settings, "scale_offset", text="")

        # ── Overview of all selected bones (when more than one) ──────────────
        if pending and len(selected) > 1:
            layout.separator()
            layout.label(text="All selected offsets:")
            by_bone = {}
            for _, offset, path, idx, bone_name in pending:
                by_bone.setdefault(bone_name, []).append((path, idx, offset))

            for bone_name, channels in by_bone.items():
                bone_box = layout.box()
                bone_box.label(text=bone_name, icon='BONE_DATA')
                for path, idx, offset in channels:
                    short = path.split('"].')[-1]
                    row = bone_box.row()
                    row.label(text=f"{short}[{idx}]")
                    row.label(text=f"{offset:+.5f}")

        # Apply button
        layout.separator()
        row = layout.row()
        row.scale_y = 1.5
        row.operator("liveoffset.apply", text="Apply to Keyframes", icon='CHECKMARK')


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

classes = (
    LIVEOFFSET_PG_settings,
    LIVEOFFSET_OT_apply,
    LIVEOFFSET_PT_panel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)

    bpy.types.WindowManager.live_offset_settings = bpy.props.PointerProperty(type=LIVEOFFSET_PG_settings)

    if _depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_update)


def unregister():
    if _depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_depsgraph_update)

    del bpy.types.WindowManager.live_offset_settings

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)