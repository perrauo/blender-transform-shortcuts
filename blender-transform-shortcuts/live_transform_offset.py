import bpy
from bpy.types import Panel, Operator, PropertyGroup
from bpy.props import FloatVectorProperty, BoolProperty, FloatProperty, EnumProperty, IntProperty
from bpy.app.handlers import persistent
import json
import math
import difflib
from collections import defaultdict


# ─────────────────────────────────────────────────────────────────────────────
# Reliable transform detection
# ─────────────────────────────────────────────────────────────────────────────

def _is_transform_running():
    for window in bpy.context.window_manager.windows:
        for op in window.modal_operators:
            if op.bl_idname.startswith("TRANSFORM_OT"):
                return True
    return False


_was_transforming = False


# ─────────────────────────────────────────────────────────────────────────────
# Core logic (shared / Live Offset)
# ─────────────────────────────────────────────────────────────────────────────

def _iter_channels(bone):
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
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        return fcurves

    anim_data = obj.animation_data
    slot = anim_data.action_slot if anim_data else None
    if slot is None:
        return None

    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            for channelbag in getattr(strip, "channelbags", []) or []:
                if getattr(channelbag, "slot_handle", None) == slot.handle:
                    return channelbag.fcurves
    return None


def _iter_all_fcurves(action):
    """Yield every F-Curve in an Action (legacy or slotted/layered)."""
    legacy = getattr(action, "fcurves", None)
    if legacy is not None:
        try:
            for fc in legacy:
                yield fc
            return
        except (TypeError, AttributeError):
            pass

    for layer in getattr(action, "layers", []) or []:
        for strip in getattr(layer, "strips", []) or []:
            bags = getattr(strip, "channelbags", None)
            if bags:
                for bag in bags:
                    yield from bag.fcurves
                continue
            # Fallback: try per-slot channelbag lookup
            for slot in getattr(action, "slots", []) or []:
                try:
                    bag = strip.channelbag(slot)
                except Exception:
                    bag = None
                if bag is not None:
                    yield from bag.fcurves


def _ensure_fcurve(action, obj, data_path, array_index):
    """
    Find or create an F-Curve on *action* for the given path/index,
    associated with *obj* (needed for slotted actions).
    Returns the F-Curve or None on failure.
    """
    # Modern Blender 4.4+ / 5.x convenience
    ensure = getattr(action, "fcurve_ensure_for_datablock", None)
    if ensure is not None and obj is not None:
        try:
            return ensure(datablock=obj, data_path=data_path, index=array_index)
        except Exception:
            pass

    # Legacy path
    fcurves = getattr(action, "fcurves", None)
    if fcurves is not None:
        fc = fcurves.find(data_path, index=array_index)
        if fc is not None:
            return fc
        try:
            return fcurves.new(data_path, index=array_index)
        except Exception:
            return None

    # Slotted / layered fallback using bpy_extras if available
    try:
        from bpy_extras import anim_utils
        anim_data = obj.animation_data if obj else None
        slot = getattr(anim_data, "action_slot", None) if anim_data else None
        if slot is None and hasattr(action, "slots") and len(action.slots):
            # Prefer a slot that matches the object, else first
            for s in action.slots:
                if getattr(s, "target_id_type", None) in ('OBJECT', 'NONE', 0):
                    slot = s
                    break
            if slot is None:
                slot = action.slots[0]
        if slot is not None:
            bag = anim_utils.action_ensure_channelbag_for_slot(action, slot)
            if bag is not None:
                fc = bag.fcurves.find(data_path, index=array_index)
                if fc is not None:
                    return fc
                return bag.fcurves.new(data_path, index=array_index)
    except Exception:
        pass

    return None


def _copy_fcurve_keyframes(src_fc, tgt_fc):
    """Replace all keyframes on tgt_fc with a full copy of those on src_fc."""
    # Clear existing keys (backwards compatible)
    while len(tgt_fc.keyframe_points):
        tgt_fc.keyframe_points.remove(tgt_fc.keyframe_points[0])

    for src_kp in src_fc.keyframe_points:
        # Insert returns the new keyframe point
        new_kp = tgt_fc.keyframe_points.insert(
            src_kp.co.x, src_kp.co.y, options={'FAST'}
        )
        # Copy handles
        new_kp.handle_left = src_kp.handle_left.copy()
        new_kp.handle_right = src_kp.handle_right.copy()
        # Copy remaining attributes that affect evaluation
        for attr in (
            "interpolation", "easing", "type",
            "back", "amplitude", "period",
            "handle_left_type", "handle_right_type",
        ):
            if hasattr(src_kp, attr) and hasattr(new_kp, attr):
                try:
                    setattr(new_kp, attr, getattr(src_kp, attr))
                except Exception:
                    pass

    # Copy a few useful F-Curve level settings
    for attr in ("extrapolation", "color_mode", "auto_smoothing"):
        if hasattr(src_fc, attr) and hasattr(tgt_fc, attr):
            try:
                setattr(tgt_fc, attr, getattr(src_fc, attr))
            except Exception:
                pass

    tgt_fc.update()


def collect_offsets(context):
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


def apply_offsets(pending, selected_only=False):
    """Shift keyframe values (and handles) by the given offsets.

    If selected_only is True, only keyframes whose control point is selected
    are modified. Otherwise every keyframe on the channel is shifted.
    """
    for fc, offset, *_ in pending:
        changed = False
        for kp in fc.keyframe_points:
            if selected_only and not kp.select_control_point:
                continue
            kp.co.y += offset
            kp.handle_left.y += offset
            kp.handle_right.y += offset
            changed = True
        if changed:
            fc.update()


def get_keyed_value(action, obj, data_path, array_index, frame):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return None
    fc = fcurves.find(data_path, index=array_index)
    if fc is None:
        return None
    return fc.evaluate(frame)


def _apply_axis_mirrors(settings, values, is_rotation=False, rotation_mode='XYZ'):
    """Apply X/Y/Z mirror toggles to a location, rotation or scale vector.

    For location / scale / Euler: simply negate the selected components.
    For Quaternion / Axis-Angle: apply the standard reflection formulas
    that preserve a valid orientation after mirroring across an axis.
    """
    if not (settings.mirror_x or settings.mirror_y or settings.mirror_z):
        return values

    vals = list(values)

    if is_rotation and rotation_mode == 'QUATERNION':
        # vals = [w, x, y, z]
        if settings.mirror_x:
            vals[2] = -vals[2]
            vals[3] = -vals[3]
        if settings.mirror_y:
            vals[1] = -vals[1]
            vals[3] = -vals[3]
        if settings.mirror_z:
            vals[1] = -vals[1]
            vals[2] = -vals[2]
    elif is_rotation and rotation_mode == 'AXIS_ANGLE':
        # vals = [angle, x, y, z] – mirror the axis vector the same way
        if settings.mirror_x:
            vals[2] = -vals[2]
            vals[3] = -vals[3]
        if settings.mirror_y:
            vals[1] = -vals[1]
            vals[3] = -vals[3]
        if settings.mirror_z:
            vals[1] = -vals[1]
            vals[2] = -vals[2]
    else:
        # Location, Scale or Euler – component-wise negation
        if settings.mirror_x and len(vals) > 0:
            vals[0] = -vals[0]
        if settings.mirror_y and len(vals) > 1:
            vals[1] = -vals[1]
        if settings.mirror_z and len(vals) > 2:
            vals[2] = -vals[2]

    return vals


# ─────────────────────────────────────────────────────────────────────────────
# Multi-bone name matching helpers
# ─────────────────────────────────────────────────────────────────────────────

def _bone_name_similarity(name_a, name_b):
    """Return a similarity ratio between two bone names (0.0 – 1.0)."""
    return difflib.SequenceMatcher(None, name_a.lower(), name_b.lower()).ratio()


def _find_best_bone_match(target_name, source_names, threshold=0.45):
    """Return the source name with the highest similarity to target_name,
    or None if no source exceeds the threshold.
    """
    best_name = None
    best_score = threshold
    for src in source_names:
        score = _bone_name_similarity(target_name, src)
        if score > best_score:
            best_score = score
            best_name = src
    return best_name


def _compute_offsets_for_bone(bone, obj, action, frame):
    """Return (loc_offset, rot_offset, scale_offset) for a single bone."""
    name = bone.name

    loc = [0.0, 0.0, 0.0]
    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        loc[i] = (bone.location[i] - keyed) if keyed is not None else bone.location[i]

    rot = [0.0, 0.0, 0.0, 0.0]
    if bone.rotation_mode == 'QUATERNION':
        q = bone.rotation_quaternion
        for i, v in enumerate((q.w, q.x, q.y, q.z)):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_quaternion', i, frame)
            rot[i] = (v - keyed) if keyed is not None else v
    elif bone.rotation_mode == 'AXIS_ANGLE':
        for i, v in enumerate(bone.rotation_axis_angle):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_axis_angle', i, frame)
            rot[i] = (v - keyed) if keyed is not None else v
    else:
        for i, v in enumerate(bone.rotation_euler):
            keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].rotation_euler', i, frame)
            rot[i] = (v - keyed) if keyed is not None else v

    scale = [0.0, 0.0, 0.0]
    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        scale[i] = (bone.scale[i] - keyed) if keyed is not None else bone.scale[i]

    return loc, rot, scale


def _apply_offset_to_bone(bone, obj, action, frame, loc_off, rot_off, scale_off):
    """Apply location / rotation / scale offsets directly to a pose bone."""
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        bone.location[i] = (keyed + loc_off[i]) if keyed is not None else loc_off[i]

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_quaternion[i] = (keyed + rot_off[i]) if keyed is not None else rot_off[i]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_axis_angle[i] = (keyed + rot_off[i]) if keyed is not None else rot_off[i]
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i in range(3):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_euler[i] = (keyed + rot_off[i]) if keyed is not None else rot_off[i]

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        bone.scale[i] = (keyed + scale_off[i]) if keyed is not None else scale_off[i]


# ─────────────────────────────────────────────────────────────────────────────
# Live Offset PropertyGroup
# ─────────────────────────────────────────────────────────────────────────────

def _update_loc(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        bone.location[i] = (keyed + self.loc_offset[i]) if keyed is not None else self.loc_offset[i]


def _update_rot(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    if bone.rotation_mode == 'QUATERNION':
        path = f'pose.bones["{name}"].rotation_quaternion'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_quaternion[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        path = f'pose.bones["{name}"].rotation_axis_angle'
        for i in range(4):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_axis_angle[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]
    else:
        path = f'pose.bones["{name}"].rotation_euler'
        for i in range(3):
            keyed = get_keyed_value(action, obj, path, i, frame)
            bone.rotation_euler[i] = (keyed + self.rot_offset[i]) if keyed is not None else self.rot_offset[i]


def _update_scale(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return
    action = obj.animation_data.action
    frame = context.scene.frame_current
    name = bone.name

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        bone.scale[i] = (keyed + self.scale_offset[i]) if keyed is not None else self.scale_offset[i]


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

    # Mirror toggles used when pasting offsets from the clipboard
    mirror_x: BoolProperty(
        name="Mirror X",
        description="Negate / reflect the X component when pasting a live offset",
        default=False,
    )
    mirror_y: BoolProperty(
        name="Mirror Y",
        description="Negate / reflect the Y component when pasting a live offset",
        default=False,
    )
    mirror_z: BoolProperty(
        name="Mirror Z",
        description="Negate / reflect the Z component when pasting a live offset",
        default=False,
    )


def sync_offsets_from_bone(context):
    if _is_transform_running():
        return

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

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
        settings.loc_offset[i] = (bone.location[i] - keyed) if keyed is not None else bone.location[i]

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

    for i in range(3):
        keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
        settings.scale_offset[i] = (bone.scale[i] - keyed) if keyed is not None else bone.scale[i]

    settings.suppress_update = False


# ─────────────────────────────────────────────────────────────────────────────
# Transform Shortcuts PropertyGroup
# ─────────────────────────────────────────────────────────────────────────────

def _update_ts_loc(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    bone.location = self.location[:]


def _update_ts_rot(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return

    if bone.rotation_mode == 'QUATERNION':
        bone.rotation_quaternion = self.rotation[:]
    elif bone.rotation_mode == 'AXIS_ANGLE':
        bone.rotation_axis_angle = self.rotation[:]
    else:
        bone.rotation_euler = self.rotation[:3]


def _update_ts_scale(self, context):
    if self.suppress_update or _is_transform_running():
        return
    bone = context.active_pose_bone
    if not bone:
        return
    bone.scale = self.scale[:]


class TRANSFORMSHORTCUTS_PG_settings(PropertyGroup):
    suppress_update: BoolProperty(default=False)

    location: FloatVectorProperty(
        name="Location",
        size=3,
        subtype='TRANSLATION',
        precision=5,
        update=_update_ts_loc,
    )
    rotation: FloatVectorProperty(
        name="Rotation",
        size=4,
        precision=5,
        update=_update_ts_rot,
    )
    scale: FloatVectorProperty(
        name="Scale",
        size=3,
        subtype='XYZ',
        default=(1.0, 1.0, 1.0),
        precision=5,
        update=_update_ts_scale,
    )


def sync_transforms_from_bone(context):
    if _is_transform_running():
        return

    wm = context.window_manager
    settings = wm.transform_shortcuts_settings
    bone = context.active_pose_bone

    if not bone:
        return

    settings.suppress_update = True

    settings.location = bone.location[:]

    if bone.rotation_mode == 'QUATERNION':
        q = bone.rotation_quaternion
        settings.rotation = (q.w, q.x, q.y, q.z)
    elif bone.rotation_mode == 'AXIS_ANGLE':
        settings.rotation = bone.rotation_axis_angle[:]
    else:
        settings.rotation = (*bone.rotation_euler[:], 0.0)

    settings.scale = bone.scale[:]

    settings.suppress_update = False


# ─────────────────────────────────────────────────────────────────────────────
# Anim Shortcuts PropertyGroup
# ─────────────────────────────────────────────────────────────────────────────

def _action_enum_items(self, context):
    """Dynamic list of all Actions in the blend file for the target dropdown."""
    items = [("NONE", "(Select Target Action)", "No target action selected")]
    for action in sorted(bpy.data.actions, key=lambda a: a.name.lower()):
        items.append((action.name, action.name, f"Copy keyframes into '{action.name}'"))
    return items


class ANIMSHORTCUTS_PG_settings(PropertyGroup):
    loop_tolerance: FloatProperty(
        name="Loop Tolerance",
        description="Maximum allowed difference per channel when matching poses",
        default=0.05,
        min=1e-7,
        max=1.0,
        soft_min=0.001,
        soft_max=0.3,
        precision=4,
        step=0.01,
    )

    prefer_period: IntProperty(
        name="Prefer Period Length",
        description="Preferred cycle length in frames. Detection will strongly prefer periods close to this value. "
                    "Set to roughly half the action length for animations that contain two walk cycles",
        default=30,
        min=4,
        max=500,
    )

    ignore_root_location: BoolProperty(
        name="Ignore Root Location",
        description="Ignore location channels of root bones (hips / pelvis / root). Essential for walk cycles",
        default=True,
    )

    compare_mode: EnumProperty(
        name="Compare",
        description="Which channels to use when detecting a loop",
        items=[
            ('ROT_SCALE', "Rotation + Scale", "Ignore all location channels (best for locomotion)"),
            ('ALL_EXCEPT_ROOT_LOC', "All except Root Loc", "Use everything except root bone location"),
            ('ALL', "Everything", "Strict: every channel of every bone must match"),
        ],
        default='ROT_SCALE',
    )

    target_action: EnumProperty(
        name="Target Action",
        description="Action that will receive the keyframes of the currently selected bones",
        items=_action_enum_items,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Safe refresh
# ─────────────────────────────────────────────────────────────────────────────

def _tag_ui_redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                for region in area.regions:
                    if region.type == 'UI':
                        region.tag_redraw()


@persistent
def _on_depsgraph_update(scene, depsgraph):
    global _was_transforming

    transforming = _is_transform_running()

    if _was_transforming and not transforming:
        try:
            sync_offsets_from_bone(bpy.context)
            sync_transforms_from_bone(bpy.context)
            _tag_ui_redraw()
        except Exception:
            pass

    _was_transforming = transforming

    if transforming:
        return

    obj = bpy.context.active_object
    if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
        return

    try:
        wm = bpy.context.window_manager
        bone = bpy.context.active_pose_bone
        frame = scene.frame_current

        fingerprint = (bone.name if bone else "", frame)
        last = getattr(wm, "_ts_last_fingerprint", None)
        if fingerprint != last:
            wm._ts_last_fingerprint = fingerprint
            sync_offsets_from_bone(bpy.context)
            sync_transforms_from_bone(bpy.context)
            _tag_ui_redraw()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Live Offset Operators
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_OT_apply(Operator):
    bl_idname = "liveoffset.apply"
    bl_label = "Apply to All Keyframes"
    bl_description = "Shift every keyframe on the affected channels by the current live offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        pending = collect_offsets(context)
        if not pending:
            self.report({'INFO'}, "Nothing to do – all offsets are zero")
            return {'CANCELLED'}

        bpy.ops.ed.undo_push(message="Live Transform Offset (All)")
        apply_offsets(pending, selected_only=False)
        context.scene.frame_set(context.scene.frame_current)
        sync_offsets_from_bone(context)

        bones = {p[4] for p in pending}
        self.report({'INFO'}, f"Shifted {len(pending)} channel(s) across {len(bones)} bone(s) (all keyframes)")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and context.selected_pose_bones and not _is_transform_running()
        )


class LIVEOFFSET_OT_apply_selected(Operator):
    bl_idname = "liveoffset.apply_selected"
    bl_label = "Apply to Selected Keyframes"
    bl_description = "Shift only the currently selected keyframes on the affected channels by the current live offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        pending = collect_offsets(context)
        if not pending:
            self.report({'INFO'}, "Nothing to do – all offsets are zero")
            return {'CANCELLED'}

        # Count how many selected keyframes will actually be touched
        selected_count = 0
        for fc, offset, *_ in pending:
            for kp in fc.keyframe_points:
                if kp.select_control_point:
                    selected_count += 1

        if selected_count == 0:
            self.report({'WARNING'}, "No selected keyframes found on the offset channels")
            return {'CANCELLED'}

        bpy.ops.ed.undo_push(message="Live Transform Offset (Selected)")
        apply_offsets(pending, selected_only=True)
        context.scene.frame_set(context.scene.frame_current)
        sync_offsets_from_bone(context)

        bones = {p[4] for p in pending}
        self.report(
            {'INFO'},
            f"Shifted {selected_count} selected keyframe(s) across {len(pending)} channel(s) / {len(bones)} bone(s)"
        )
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and context.selected_pose_bones and not _is_transform_running()
        )


class LIVEOFFSET_OT_refresh(Operator):
    bl_idname = "liveoffset.refresh"
    bl_label = "Refresh Offsets"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not _is_transform_running():
            sync_offsets_from_bone(context)
        return {'FINISHED'}


class LIVEOFFSET_OT_reset(Operator):
    bl_idname = "liveoffset.reset"
    bl_label = "Reset Offsets"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings
        settings.suppress_update = True
        settings.loc_offset = (0.0, 0.0, 0.0)
        settings.rot_offset = (0.0, 0.0, 0.0, 0.0)
        settings.scale_offset = (0.0, 0.0, 0.0)
        settings.suppress_update = False

        _update_loc(settings, context)
        _update_rot(settings, context)
        _update_scale(settings, context)

        self.report({'INFO'}, "Offsets reset to zero")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class LIVEOFFSET_OT_copy(Operator):
    bl_idname = "liveoffset.copy"
    bl_label = "Copy Offsets"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)
        selected = context.selected_pose_bones or []

        if not selected:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}

        bones_data = {}
        for bone in selected:
            loc, rot, scale = _compute_offsets_for_bone(bone, obj, action, frame)
            bones_data[bone.name] = {
                "loc": loc,
                "rot": rot,
                "scale": scale,
            }

        data = {
            "live_offset": True,
            "bones": bones_data,
        }

        # Keep old flat format when only one bone is selected (backward compatible)
        if len(selected) == 1:
            name = selected[0].name
            data["loc"] = bones_data[name]["loc"]
            data["rot"] = bones_data[name]["rot"]
            data["scale"] = bones_data[name]["scale"]

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            if len(selected) == 1:
                self.report({'INFO'}, "Offsets copied to clipboard")
            else:
                self.report({'INFO'}, f"Offsets for {len(selected)} bones copied to clipboard")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.mode == 'POSE' and not _is_transform_running()


class LIVEOFFSET_OT_paste(Operator):
    bl_idname = "liveoffset.paste"
    bl_label = "Paste Offsets"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("live_offset"):
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)

        # ── Multi-bone paste (only when more than one bone was copied) ──────
        bones_data = data.get("bones")
        if isinstance(bones_data, dict) and len(bones_data) > 1:
            selected = context.selected_pose_bones or []
            if not selected:
                self.report({'WARNING'}, "Select target bones to paste multi-bone offsets")
                return {'CANCELLED'}

            source_names = list(bones_data.keys())
            matched = 0
            skipped = 0

            for bone in selected:
                best = _find_best_bone_match(bone.name, source_names)
                if best is None:
                    skipped += 1
                    continue

                src = bones_data[best]
                loc = _apply_axis_mirrors(settings, src.get("loc", [0, 0, 0]))
                rot = _apply_axis_mirrors(
                    settings, src.get("rot", [0, 0, 0, 0]),
                    is_rotation=True, rotation_mode=bone.rotation_mode
                )
                scale = _apply_axis_mirrors(settings, src.get("scale", [0, 0, 0]))

                _apply_offset_to_bone(bone, obj, action, frame, loc, rot, scale)
                matched += 1

            sync_offsets_from_bone(context)

            msg = f"Pasted offsets to {matched} bone(s) via name matching"
            if skipped:
                msg += f" ({skipped} unmatched)"
            self.report({'INFO'}, msg)
            return {'FINISHED'}

        # ── Single-bone / legacy paste ──────────────────────────────────────
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        rot_mode = bone.rotation_mode

        # Prefer explicit bones dict with a single entry, fall back to flat keys
        if isinstance(bones_data, dict) and len(bones_data) == 1:
            src = next(iter(bones_data.values()))
            loc_src = src.get("loc")
            rot_src = src.get("rot")
            scale_src = src.get("scale")
        else:
            loc_src = data.get("loc")
            rot_src = data.get("rot")
            scale_src = data.get("scale")

        settings.suppress_update = True
        try:
            if loc_src and len(loc_src) == 3:
                settings.loc_offset = _apply_axis_mirrors(settings, loc_src)
            if rot_src and len(rot_src) == 4:
                settings.rot_offset = _apply_axis_mirrors(
                    settings, rot_src, is_rotation=True, rotation_mode=rot_mode
                )
            if scale_src and len(scale_src) == 3:
                settings.scale_offset = _apply_axis_mirrors(settings, scale_src)
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_loc(settings, context)
        _update_rot(settings, context)
        _update_scale(settings, context)

        self.report({'INFO'}, "Offsets pasted from clipboard")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


# Individual Live Offset Copy / Paste operators

class LIVEOFFSET_OT_copy_location(Operator):
    bl_idname = "liveoffset.copy_location"
    bl_label = "Copy Location Offset"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)
        selected = context.selected_pose_bones or []

        if not selected:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}

        bones_data = {}
        for bone in selected:
            loc, _, _ = _compute_offsets_for_bone(bone, obj, action, frame)
            bones_data[bone.name] = {"loc": loc}

        data = {
            "live_offset": True,
            "bones": bones_data,
        }
        if len(selected) == 1:
            data["loc"] = bones_data[selected[0].name]["loc"]

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            if len(selected) == 1:
                self.report({'INFO'}, "Location offset copied")
            else:
                self.report({'INFO'}, f"Location offsets for {len(selected)} bones copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.mode == 'POSE' and not _is_transform_running()


class LIVEOFFSET_OT_paste_location(Operator):
    bl_idname = "liveoffset.paste_location"
    bl_label = "Paste Location Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("live_offset"):
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)

        bones_data = data.get("bones")
        if isinstance(bones_data, dict) and len(bones_data) > 1:
            selected = context.selected_pose_bones or []
            if not selected:
                self.report({'WARNING'}, "Select target bones to paste multi-bone offsets")
                return {'CANCELLED'}

            source_names = list(bones_data.keys())
            matched = 0
            for bone in selected:
                best = _find_best_bone_match(bone.name, source_names)
                if best is None:
                    continue
                src = bones_data[best]
                if "loc" not in src or len(src["loc"]) != 3:
                    continue
                loc = _apply_axis_mirrors(settings, src["loc"])
                name = bone.name
                for i in range(3):
                    keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].location', i, frame)
                    bone.location[i] = (keyed + loc[i]) if keyed is not None else loc[i]
                matched += 1

            sync_offsets_from_bone(context)
            self.report({'INFO'}, f"Location offsets pasted to {matched} bone(s)")
            return {'FINISHED'}

        # Single / legacy
        if isinstance(bones_data, dict) and len(bones_data) == 1:
            loc_src = next(iter(bones_data.values())).get("loc")
        else:
            loc_src = data.get("loc")

        if not loc_src or len(loc_src) != 3:
            self.report({'WARNING'}, "Clipboard does not contain a valid Location Offset")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            settings.loc_offset = _apply_axis_mirrors(settings, loc_src)
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_loc(settings, context)
        self.report({'INFO'}, "Location offset pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class LIVEOFFSET_OT_copy_rotation(Operator):
    bl_idname = "liveoffset.copy_rotation"
    bl_label = "Copy Rotation Offset"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)
        selected = context.selected_pose_bones or []

        if not selected:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}

        bones_data = {}
        for bone in selected:
            _, rot, _ = _compute_offsets_for_bone(bone, obj, action, frame)
            bones_data[bone.name] = {"rot": rot}

        data = {
            "live_offset": True,
            "bones": bones_data,
        }
        if len(selected) == 1:
            data["rot"] = bones_data[selected[0].name]["rot"]

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            if len(selected) == 1:
                self.report({'INFO'}, "Rotation offset copied")
            else:
                self.report({'INFO'}, f"Rotation offsets for {len(selected)} bones copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.mode == 'POSE' and not _is_transform_running()


class LIVEOFFSET_OT_paste_rotation(Operator):
    bl_idname = "liveoffset.paste_rotation"
    bl_label = "Paste Rotation Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("live_offset"):
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)

        bones_data = data.get("bones")
        if isinstance(bones_data, dict) and len(bones_data) > 1:
            selected = context.selected_pose_bones or []
            if not selected:
                self.report({'WARNING'}, "Select target bones to paste multi-bone offsets")
                return {'CANCELLED'}

            source_names = list(bones_data.keys())
            matched = 0
            for bone in selected:
                best = _find_best_bone_match(bone.name, source_names)
                if best is None:
                    continue
                src = bones_data[best]
                if "rot" not in src or len(src["rot"]) != 4:
                    continue
                rot = _apply_axis_mirrors(
                    settings, src["rot"],
                    is_rotation=True, rotation_mode=bone.rotation_mode
                )
                name = bone.name
                if bone.rotation_mode == 'QUATERNION':
                    path = f'pose.bones["{name}"].rotation_quaternion'
                    for i in range(4):
                        keyed = get_keyed_value(action, obj, path, i, frame)
                        bone.rotation_quaternion[i] = (keyed + rot[i]) if keyed is not None else rot[i]
                elif bone.rotation_mode == 'AXIS_ANGLE':
                    path = f'pose.bones["{name}"].rotation_axis_angle'
                    for i in range(4):
                        keyed = get_keyed_value(action, obj, path, i, frame)
                        bone.rotation_axis_angle[i] = (keyed + rot[i]) if keyed is not None else rot[i]
                else:
                    path = f'pose.bones["{name}"].rotation_euler'
                    for i in range(3):
                        keyed = get_keyed_value(action, obj, path, i, frame)
                        bone.rotation_euler[i] = (keyed + rot[i]) if keyed is not None else rot[i]
                matched += 1

            sync_offsets_from_bone(context)
            self.report({'INFO'}, f"Rotation offsets pasted to {matched} bone(s)")
            return {'FINISHED'}

        # Single / legacy
        if isinstance(bones_data, dict) and len(bones_data) == 1:
            rot_src = next(iter(bones_data.values())).get("rot")
        else:
            rot_src = data.get("rot")

        if not rot_src or len(rot_src) != 4:
            self.report({'WARNING'}, "Clipboard does not contain a valid Rotation Offset")
            return {'CANCELLED'}

        bone = context.active_pose_bone
        rot_mode = bone.rotation_mode if bone else 'XYZ'

        settings.suppress_update = True
        try:
            settings.rot_offset = _apply_axis_mirrors(
                settings, rot_src, is_rotation=True, rotation_mode=rot_mode
            )
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_rot(settings, context)
        self.report({'INFO'}, "Rotation offset pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class LIVEOFFSET_OT_copy_scale(Operator):
    bl_idname = "liveoffset.copy_scale"
    bl_label = "Copy Scale Offset"
    bl_options = {'REGISTER'}

    def execute(self, context):
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)
        selected = context.selected_pose_bones or []

        if not selected:
            self.report({'WARNING'}, "No bones selected")
            return {'CANCELLED'}

        bones_data = {}
        for bone in selected:
            _, _, scale = _compute_offsets_for_bone(bone, obj, action, frame)
            bones_data[bone.name] = {"scale": scale}

        data = {
            "live_offset": True,
            "bones": bones_data,
        }
        if len(selected) == 1:
            data["scale"] = bones_data[selected[0].name]["scale"]

        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            if len(selected) == 1:
                self.report({'INFO'}, "Scale offset copied")
            else:
                self.report({'INFO'}, f"Scale offsets for {len(selected)} bones copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return obj and obj.type == 'ARMATURE' and obj.mode == 'POSE' and not _is_transform_running()


class LIVEOFFSET_OT_paste_scale(Operator):
    bl_idname = "liveoffset.paste_scale"
    bl_label = "Paste Scale Offset"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("live_offset"):
            self.report({'WARNING'}, "Clipboard does not contain valid Live Offset data")
            return {'CANCELLED'}

        settings = context.window_manager.live_offset_settings
        obj = context.active_object
        if not obj or not obj.animation_data or not obj.animation_data.action:
            self.report({'WARNING'}, "No active Action")
            return {'CANCELLED'}

        action = obj.animation_data.action
        frame = float(context.scene.frame_current)

        bones_data = data.get("bones")
        if isinstance(bones_data, dict) and len(bones_data) > 1:
            selected = context.selected_pose_bones or []
            if not selected:
                self.report({'WARNING'}, "Select target bones to paste multi-bone offsets")
                return {'CANCELLED'}

            source_names = list(bones_data.keys())
            matched = 0
            for bone in selected:
                best = _find_best_bone_match(bone.name, source_names)
                if best is None:
                    continue
                src = bones_data[best]
                if "scale" not in src or len(src["scale"]) != 3:
                    continue
                scale = _apply_axis_mirrors(settings, src["scale"])
                name = bone.name
                for i in range(3):
                    keyed = get_keyed_value(action, obj, f'pose.bones["{name}"].scale', i, frame)
                    bone.scale[i] = (keyed + scale[i]) if keyed is not None else scale[i]
                matched += 1

            sync_offsets_from_bone(context)
            self.report({'INFO'}, f"Scale offsets pasted to {matched} bone(s)")
            return {'FINISHED'}

        # Single / legacy
        if isinstance(bones_data, dict) and len(bones_data) == 1:
            scale_src = next(iter(bones_data.values())).get("scale")
        else:
            scale_src = data.get("scale")

        if not scale_src or len(scale_src) != 3:
            self.report({'WARNING'}, "Clipboard does not contain a valid Scale Offset")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            settings.scale_offset = _apply_axis_mirrors(settings, scale_src)
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_scale(settings, context)
        self.report({'INFO'}, "Scale offset pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Transform Shortcuts Operators
# ─────────────────────────────────────────────────────────────────────────────

class TRANSFORMSHORTCUTS_OT_copy(Operator):
    bl_idname = "transformshortcuts.copy"
    bl_label = "Copy Transform"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        data = {
            "transform_shortcuts": True,
            "location": list(settings.location),
            "rotation": list(settings.rotation),
            "scale":    list(settings.scale),
            "rotation_mode": bone.rotation_mode if bone else "XYZ",
        }
        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Transform copied to clipboard")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_paste(Operator):
    bl_idname = "transformshortcuts.paste"
    bl_label = "Paste Transform"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("transform_shortcuts"):
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            if "location" in data and len(data["location"]) == 3:
                settings.location = data["location"]
            if "rotation" in data and len(data["rotation"]) == 4:
                settings.rotation = data["rotation"]
            if "scale" in data and len(data["scale"]) == 3:
                settings.scale = data["scale"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_ts_loc(settings, context)
        _update_ts_rot(settings, context)
        _update_ts_scale(settings, context)

        self.report({'INFO'}, "Transform pasted from clipboard")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_refresh(Operator):
    bl_idname = "transformshortcuts.refresh"
    bl_label = "Refresh Transform"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        if not _is_transform_running():
            sync_transforms_from_bone(context)
        return {'FINISHED'}


# Individual Transform Shortcuts Copy / Paste operators

class TRANSFORMSHORTCUTS_OT_copy_location(Operator):
    bl_idname = "transformshortcuts.copy_location"
    bl_label = "Copy Location"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.transform_shortcuts_settings
        data = {
            "transform_shortcuts": True,
            "location": list(settings.location),
        }
        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Location copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_paste_location(Operator):
    bl_idname = "transformshortcuts.paste_location"
    bl_label = "Paste Location"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("transform_shortcuts") or "location" not in data or len(data["location"]) != 3:
            self.report({'WARNING'}, "Clipboard does not contain a valid Location")
            return {'CANCELLED'}

        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            settings.location = data["location"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_ts_loc(settings, context)
        self.report({'INFO'}, "Location pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_copy_rotation(Operator):
    bl_idname = "transformshortcuts.copy_rotation"
    bl_label = "Copy Rotation"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        data = {
            "transform_shortcuts": True,
            "rotation": list(settings.rotation),
            "rotation_mode": bone.rotation_mode if bone else "XYZ",
        }
        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Rotation copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_paste_rotation(Operator):
    bl_idname = "transformshortcuts.paste_rotation"
    bl_label = "Paste Rotation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("transform_shortcuts") or "rotation" not in data or len(data["rotation"]) != 4:
            self.report({'WARNING'}, "Clipboard does not contain a valid Rotation")
            return {'CANCELLED'}

        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            settings.rotation = data["rotation"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_ts_rot(settings, context)
        self.report({'INFO'}, "Rotation pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_copy_scale(Operator):
    bl_idname = "transformshortcuts.copy_scale"
    bl_label = "Copy Scale"
    bl_options = {'REGISTER'}

    def execute(self, context):
        settings = context.window_manager.transform_shortcuts_settings
        data = {
            "transform_shortcuts": True,
            "scale": list(settings.scale),
        }
        try:
            context.window_manager.clipboard = json.dumps(data, separators=(',', ':'))
            self.report({'INFO'}, "Scale copied")
        except Exception as e:
            self.report({'ERROR'}, f"Copy failed: {e}")
            return {'CANCELLED'}
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


class TRANSFORMSHORTCUTS_OT_paste_scale(Operator):
    bl_idname = "transformshortcuts.paste_scale"
    bl_label = "Paste Scale"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        clipboard = context.window_manager.clipboard.strip()
        if not clipboard:
            self.report({'WARNING'}, "Clipboard is empty")
            return {'CANCELLED'}

        try:
            data = json.loads(clipboard)
        except json.JSONDecodeError:
            self.report({'WARNING'}, "Clipboard does not contain valid Transform data")
            return {'CANCELLED'}

        if not isinstance(data, dict) or not data.get("transform_shortcuts") or "scale" not in data or len(data["scale"]) != 3:
            self.report({'WARNING'}, "Clipboard does not contain a valid Scale")
            return {'CANCELLED'}

        settings = context.window_manager.transform_shortcuts_settings
        bone = context.active_pose_bone
        if not bone:
            self.report({'WARNING'}, "No active pose bone")
            return {'CANCELLED'}

        settings.suppress_update = True
        try:
            settings.scale = data["scale"]
        except Exception as e:
            settings.suppress_update = False
            self.report({'ERROR'}, f"Paste failed: {e}")
            return {'CANCELLED'}

        settings.suppress_update = False
        _update_ts_scale(settings, context)
        self.report({'INFO'}, "Scale pasted")
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and context.active_pose_bone and not _is_transform_running()
        )


# ─────────────────────────────────────────────────────────────────────────────
# Anim Shortcuts – Loop Detection (prefer-period aware)
# ─────────────────────────────────────────────────────────────────────────────

_MIN_LOOP_FRAMES = 8          # never accept extremely short periods
_ROOT_NAME_PATTERNS = (
    "root", "hips", "hip", "pelvis", "cog", "center", "master",
    "armature", "origin", "motion", "mover", "body", "torso"
)


def _is_root_bone(bone_name):
    lower = bone_name.lower()
    return any(p in lower for p in _ROOT_NAME_PATTERNS)


def _bone_name_from_path(data_path):
    if data_path.startswith('pose.bones["'):
        end = data_path.find('"]')
        if end != -1:
            return data_path[12:end]
    return data_path


def _is_location_channel(data_path):
    return ".location" in data_path


def _should_compare_channel(data_path, settings):
    mode = settings.compare_mode
    bone = _bone_name_from_path(data_path)
    is_loc = _is_location_channel(data_path)

    if mode == 'ALL':
        return True
    if mode == 'ROT_SCALE':
        return not is_loc
    if mode == 'ALL_EXCEPT_ROOT_LOC':
        if is_loc and settings.ignore_root_location and _is_root_bone(bone):
            return False
        return True
    return True


def _collect_pose_fcurves(action, obj, settings):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return []

    result = []
    for fc in fcurves:
        if not fc.data_path.startswith('pose.bones[') or fc.lock or fc.mute:
            continue
        if _should_compare_channel(fc.data_path, settings):
            result.append((fc, fc.data_path, fc.array_index))
    return result


def _get_action_frame_range(fcurves):
    min_f = float('inf')
    max_f = float('-inf')
    has_keys = False
    for fc, _, _ in fcurves:
        for kp in fc.keyframe_points:
            t = kp.co[0]
            min_f = min(min_f, t)
            max_f = max(max_f, t)
            has_keys = True
    if not has_keys:
        return None
    return (math.floor(min_f), math.ceil(max_f))


def _channel_diff(fc, frame_a, frame_b):
    return abs(fc.evaluate(frame_a) - fc.evaluate(frame_b))


def _find_breaking_channels(fcurves, start, period, end, tol):
    breaking = defaultdict(list)
    for fc, data_path, array_index in fcurves:
        max_diff = 0.0
        worst_frame = start
        for f in range(int(start), int(start + period)):
            if f + period > end:
                break
            d = _channel_diff(fc, f, f + period)
            if d > max_diff:
                max_diff = d
                worst_frame = f
        if max_diff > tol:
            bone = _bone_name_from_path(data_path)
            short = data_path.split('"].')[-1] if '"].' in data_path else data_path
            breaking[bone].append((short, array_index, max_diff, worst_frame))
    return dict(breaking)


def _pose_matches(fcurves, frame_a, frame_b, tol):
    for fc, _, _ in fcurves:
        if _channel_diff(fc, frame_a, frame_b) > tol:
            return False
    return True


def _is_valid_period(fcurves, start, period, end, tol):
    if period < _MIN_LOOP_FRAMES:
        return False
    if start + period > end:
        return False
    if not _pose_matches(fcurves, start, start + period, tol):
        return False
    for f in range(int(start), int(start + period)):
        if f + period > end:
            break
        if not _pose_matches(fcurves, f, f + period, tol):
            return False
    return True


def _score_period(period, prefer, n_breaking, total_bones):
    """
    Lower score = better.
    Heavily rewards being close to the preferred period and having few breaking bones.
    """
    # Distance from preferred (normalised)
    dist = abs(period - prefer) / max(prefer, 1)
    # Breaking ratio
    break_ratio = n_breaking / max(total_bones, 1)
    # Combined score (distance is weighted higher so we prefer the right length)
    return dist * 3.0 + break_ratio


def detect_loop_period(context, tol=None):
    """Return (start, period) for a perfect loop near the preferred length, or (None, None)."""
    settings = context.window_manager.anim_shortcuts_settings
    if tol is None:
        tol = settings.loop_tolerance
    prefer = settings.prefer_period

    obj = context.active_object
    if not obj or obj.type != 'ARMATURE' or not obj.animation_data or not obj.animation_data.action:
        return None, None

    action = obj.animation_data.action
    fcurves = _collect_pose_fcurves(action, obj, settings)
    if not fcurves:
        return None, None

    frame_range = _get_action_frame_range(fcurves)
    if frame_range is None:
        return None, None

    start, end = frame_range
    total_len = end - start
    if total_len < _MIN_LOOP_FRAMES * 2:
        return None, None

    # Search a window around the preferred period
    search_min = max(_MIN_LOOP_FRAMES, int(prefer * 0.6))
    search_max = min(int(total_len // 2), int(prefer * 1.6) + 1)

    best = None  # (score, period)

    for period in range(search_min, search_max + 1):
        if start + period > end:
            continue
        if _is_valid_period(fcurves, start, period, end, tol):
            score = _score_period(period, prefer, 0, 1)
            if best is None or score < best[0]:
                best = (score, period)

    if best is None:
        return None, None
    return start, best[1]


def find_best_almost_loop(context, tol=None):
    """
    Find the best period near the preferred length even if not perfect.
    Returns (start, period, breaking_dict).
    """
    settings = context.window_manager.anim_shortcuts_settings
    if tol is None:
        tol = settings.loop_tolerance
    prefer = settings.prefer_period

    obj = context.active_object
    if not obj or obj.type != 'ARMATURE' or not obj.animation_data or not obj.animation_data.action:
        return None, None, {}

    action = obj.animation_data.action
    fcurves = _collect_pose_fcurves(action, obj, settings)
    if not fcurves:
        return None, None, {}

    frame_range = _get_action_frame_range(fcurves)
    if frame_range is None:
        return None, None, {}

    start, end = frame_range
    total_len = end - start
    if total_len < _MIN_LOOP_FRAMES * 2:
        return None, None, {}

    total_bones = len({_bone_name_from_path(dp) for _, dp, _ in fcurves})

    search_min = max(_MIN_LOOP_FRAMES, int(prefer * 0.5))
    search_max = min(int(total_len // 2), int(prefer * 1.8) + 1)

    best = None  # (score, period, breaking)

    for period in range(search_min, search_max + 1):
        if start + period > end:
            continue
        breaking = _find_breaking_channels(fcurves, start, period, end, tol)
        n_break = len(breaking)
        score = _score_period(period, prefer, n_break, total_bones)

        if best is None or score < best[0]:
            best = (score, period, breaking)

        # Early perfect match near preferred
        if n_break == 0 and abs(period - prefer) < prefer * 0.15:
            break

    if best is None:
        return None, None, {}
    return start, best[1], best[2]


def get_action_total_length(context):
    """Helper: return total keyed length of the active action."""
    obj = context.active_object
    if not obj or not obj.animation_data or not obj.animation_data.action:
        return 0
    settings = context.window_manager.anim_shortcuts_settings
    fcurves = _collect_pose_fcurves(obj.animation_data.action, obj, settings)
    if not fcurves:
        # fallback – look at all fcurves
        fcurves = _get_action_fcurves(obj.animation_data.action, obj) or []
        fcurves = [(fc, fc.data_path, fc.array_index) for fc in fcurves]
    fr = _get_action_frame_range(fcurves)
    if fr is None:
        return 0
    return fr[1] - fr[0]


def truncate_after_loop(action, obj, start, period):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return 0
    cutoff = start + period
    removed = 0
    for fc in fcurves:
        for i in range(len(fc.keyframe_points) - 1, -1, -1):
            if fc.keyframe_points[i].co[0] > cutoff + 1e-6:
                fc.keyframe_points.remove(fc.keyframe_points[i])
                removed += 1
        fc.update()
    return removed


def homogenise_to_period(action, obj, start, period, settings):
    fcurves = _get_action_fcurves(action, obj)
    if fcurves is None:
        return 0, set()

    adjusted = 0
    bones = set()

    for fc in fcurves:
        if not fc.data_path.startswith('pose.bones[') or fc.lock or fc.mute:
            continue
        # Preserve progressive root motion
        if (settings.ignore_root_location
                and _is_location_channel(fc.data_path)
                and _is_root_bone(_bone_name_from_path(fc.data_path))):
            continue

        bone = _bone_name_from_path(fc.data_path)
        changed = False
        for kp in fc.keyframe_points:
            t = kp.co[0]
            if t < start - 1e-6:
                continue
            phase = (t - start) % period
            ref_frame = start + phase
            if t > start + period - 1e-6 or abs(t - (start + period)) < 1e-4:
                ref_val = fc.evaluate(ref_frame)
                dy = ref_val - kp.co[1]
                if abs(dy) > 1e-9:
                    kp.co[1] = ref_val
                    kp.handle_left[1] += dy
                    kp.handle_right[1] += dy
                    adjusted += 1
                    changed = True
        if changed:
            bones.add(bone)
            fc.update()
    return adjusted, bones


# ─────────────────────────────────────────────────────────────────────────────
# Anim Shortcuts Operators
# ─────────────────────────────────────────────────────────────────────────────

class ANIMSHORTCUTS_OT_set_prefer_half(Operator):
    """Set Prefer Period Length to half the total action length"""
    bl_idname = "animshortcuts.set_prefer_half"
    bl_label = "Set to Half Length"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        total = get_action_total_length(context)
        if total < 4:
            self.report({'WARNING'}, "Action is too short")
            return {'CANCELLED'}
        settings = context.window_manager.anim_shortcuts_settings
        settings.prefer_period = max(4, int(round(total / 2)))
        self.report({'INFO'}, f"Prefer Period set to {settings.prefer_period} (half of {total})")
        return {'FINISHED'}


class ANIMSHORTCUTS_OT_detect_loop(Operator):
    bl_idname = "animshortcuts.detect_loop"
    bl_label = "Detect Loop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.window_manager.anim_shortcuts_settings
        tol = settings.loop_tolerance

        start, period = detect_loop_period(context, tol=tol)

        if start is not None and period is not None:
            scene = context.scene
            scene.frame_start = int(start)
            scene.frame_end = int(start + period)
            scene.frame_preview_start = int(start)
            scene.frame_preview_end = int(start + period)
            scene.use_preview_range = True

            self.report(
                {'INFO'},
                f"Loop detected: frames {int(start)} → {int(start + period)} "
                f"(period = {period}, preferred = {settings.prefer_period})"
            )
            return {'FINISHED'}

        # Diagnostics with preferred-period scoring
        start, period, breaking = find_best_almost_loop(context, tol=tol)
        if start is None:
            self.report({'WARNING'}, "Could not find any usable loop candidate.")
            return {'CANCELLED'}

        print("\n========== LOOP DIAGNOSTICS ==========")
        print(f"Preferred period      : {settings.prefer_period}")
        print(f"Best candidate period : {period}")
        print(f"Tolerance             : {tol:.4g}")
        print(f"Compare mode          : {settings.compare_mode}")
        print(f"Bones still breaking ({len(breaking)}):")
        for bone, channels in sorted(breaking.items()):
            print(f"  • {bone}")
            for short, idx, max_diff, worst_f in channels:
                print(f"      {short}[{idx}]  max Δ = {max_diff:.4g}  (frame {worst_f})")
        print("======================================\n")

        bone_list = ", ".join(sorted(breaking.keys())[:8])
        if len(breaking) > 8:
            bone_list += f" … (+{len(breaking)-8} more)"

        self.report(
            {'WARNING'},
            f"No perfect loop. Best period={period} (preferred={settings.prefer_period}). "
            f"Breaking bones ({len(breaking)}): {bone_list}. See System Console."
        )
        return {'CANCELLED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and not _is_transform_running()
        )


class ANIMSHORTCUTS_OT_detect_truncate_loop(Operator):
    bl_idname = "animshortcuts.detect_truncate_loop"
    bl_label = "Detect and Truncate Loop"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.window_manager.anim_shortcuts_settings
        tol = settings.loop_tolerance

        start, period = detect_loop_period(context, tol=tol)
        if start is None:
            start, period, breaking = find_best_almost_loop(context, tol=tol)
            if start is None:
                self.report({'WARNING'}, "No usable loop found. Adjust Prefer Period or tolerance.")
                return {'CANCELLED'}
            self.report(
                {'WARNING'},
                f"No perfect loop – truncating best candidate (period={period}). "
                f"{len(breaking)} bone(s) still differ."
            )

        obj = context.active_object
        action = obj.animation_data.action
        bpy.ops.ed.undo_push(message="Detect and Truncate Loop")

        removed = truncate_after_loop(action, obj, start, period)

        scene = context.scene
        scene.frame_start = int(start)
        scene.frame_end = int(start + period)
        scene.frame_preview_start = int(start)
        scene.frame_preview_end = int(start + period)
        scene.use_preview_range = True
        context.scene.frame_set(context.scene.frame_current)

        self.report(
            {'INFO'},
            f"Truncated to frames {int(start)} → {int(start + period)} "
            f"(period = {period}, removed {removed} keys)"
        )
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and not _is_transform_running()
        )


class ANIMSHORTCUTS_OT_homogenise(Operator):
    bl_idname = "animshortcuts.homogenise"
    bl_label = "Homogenise Animation"
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        settings = context.window_manager.anim_shortcuts_settings
        tol = settings.loop_tolerance

        start, period = detect_loop_period(context, tol=tol)
        if start is None:
            start, period, _ = find_best_almost_loop(context, tol=tol)

        if start is None or period is None:
            self.report({'WARNING'}, "Could not determine a usable loop period.")
            return {'CANCELLED'}

        obj = context.active_object
        action = obj.animation_data.action
        bpy.ops.ed.undo_push(message="Homogenise Animation")

        adjusted, bones = homogenise_to_period(action, obj, start, period, settings)
        removed = truncate_after_loop(action, obj, start, period)

        scene = context.scene
        scene.frame_start = int(start)
        scene.frame_end = int(start + period)
        scene.frame_preview_start = int(start)
        scene.frame_preview_end = int(start + period)
        scene.use_preview_range = True
        context.scene.frame_set(context.scene.frame_current)

        bone_list = ", ".join(sorted(bones)[:8])
        if len(bones) > 8:
            bone_list += f" … (+{len(bones)-8} more)"

        self.report(
            {'INFO'},
            f"Homogenised to period {period}. Adjusted {adjusted} keys on {len(bones)} bones"
            + (f" ({bone_list})" if bones else "")
            + f". Removed {removed} extra keys."
        )
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and not _is_transform_running()
        )


class ANIMSHORTCUTS_OT_copy_bones_to_action(Operator):
    """Copy all keyframes of the selected pose bones from the active Action
    into the Action chosen in the Target Action dropdown.
    Existing keyframes on matching channels in the target are replaced.
    """
    bl_idname = "animshortcuts.copy_bones_to_action"
    bl_label = "Copy Selected Bones Keyframes"
    bl_description = (
        "Copy every keyframe belonging to the currently selected bones "
        "from the active Action into the Target Action selected above"
    )
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        if _is_transform_running():
            self.report({'WARNING'}, "Finish the transform first")
            return {'CANCELLED'}

        obj = context.active_object
        if not obj or obj.type != 'ARMATURE' or obj.mode != 'POSE':
            self.report({'WARNING'}, "Select an Armature in Pose Mode")
            return {'CANCELLED'}

        anim_data = obj.animation_data
        if not anim_data or not anim_data.action:
            self.report({'WARNING'}, "No active Action on the armature")
            return {'CANCELLED'}

        src_action = anim_data.action
        settings = context.window_manager.anim_shortcuts_settings
        target_name = settings.target_action

        if not target_name or target_name == "NONE":
            self.report({'WARNING'}, "Choose a Target Action in the dropdown")
            return {'CANCELLED'}

        tgt_action = bpy.data.actions.get(target_name)
        if tgt_action is None:
            self.report({'WARNING'}, f"Action '{target_name}' no longer exists")
            return {'CANCELLED'}

        if tgt_action == src_action:
            self.report({'WARNING'}, "Source and Target are the same Action")
            return {'CANCELLED'}

        selected = context.selected_pose_bones or []
        if not selected:
            self.report({'WARNING'}, "Select one or more bones first")
            return {'CANCELLED'}

        # Build set of data-path prefixes for the selected bones
        bone_prefixes = {
            f'pose.bones["{bone.name}"]' for bone in selected
        }

        # Collect source F-Curves that belong to the selected bones
        src_fcurves = list(_iter_all_fcurves(src_action))
        to_copy = []
        for fc in src_fcurves:
            path = fc.data_path
            if any(path.startswith(prefix) for prefix in bone_prefixes):
                to_copy.append(fc)

        if not to_copy:
            self.report({'INFO'}, "No keyframes found on the selected bones in the active Action")
            return {'CANCELLED'}

        bpy.ops.ed.undo_push(message="Copy Bones Keyframes to Action")

        copied_channels = 0
        skipped = 0
        bones_touched = set()

        for src_fc in to_copy:
            tgt_fc = _ensure_fcurve(
                tgt_action, obj, src_fc.data_path, src_fc.array_index
            )
            if tgt_fc is None:
                skipped += 1
                continue

            _copy_fcurve_keyframes(src_fc, tgt_fc)
            copied_channels += 1

            # Extract bone name for reporting
            bone = _bone_name_from_path(src_fc.data_path)
            if bone:
                bones_touched.add(bone)

        msg = (
            f"Copied {copied_channels} channel(s) for {len(bones_touched)} bone(s) "
            f"from '{src_action.name}' → '{tgt_action.name}'"
        )
        if skipped:
            msg += f" ({skipped} channel(s) could not be created)"
        self.report({'INFO'}, msg)
        return {'FINISHED'}

    @classmethod
    def poll(cls, context):
        obj = context.active_object
        return (
            obj and obj.type == 'ARMATURE' and obj.mode == 'POSE'
            and obj.animation_data and obj.animation_data.action
            and context.selected_pose_bones
            and not _is_transform_running()
        )


# ─────────────────────────────────────────────────────────────────────────────
# UI Panels
# ─────────────────────────────────────────────────────────────────────────────

class LIVEOFFSET_PT_panel(Panel):
    bl_label = "Live Offset"
    bl_idname = "LIVEOFFSET_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Transform Shortcuts"

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

        transforming = _is_transform_running()
        settings = wm.live_offset_settings
        pending = collect_offsets(context)

        box = layout.box()
        box.label(text=f"Selected: {len(selected)} bone(s)", icon='BONE_DATA')
        if transforming:
            box.label(text="Transforming… (offsets frozen)", icon='TIME')
        elif not pending:
            box.label(text="All offsets ≈ 0", icon='CHECKMARK')
        else:
            box.label(text=f"{len(pending)} channel(s) with offset", icon='MODIFIER')

        # Mirror toggles for paste operations (like viewport mirror buttons)
        mirror_box = layout.box()
        mirror_box.label(text="Paste Mirror", icon='MOD_MIRROR')
        mirror_box.enabled = not transforming
        row = mirror_box.row(align=True)
        row.prop(settings, "mirror_x", text="X", toggle=True)
        row.prop(settings, "mirror_y", text="Y", toggle=True)
        row.prop(settings, "mirror_z", text="Z", toggle=True)

        bone = context.active_pose_bone
        if bone:
            row = layout.row()
            row.label(text="Rotation Mode")
            row.prop(bone, "rotation_mode", text="")

            # ── Location Offset ──────────────────────────────────────────────
            box = layout.box()
            box.label(text="Location Offset")
            box.enabled = not transforming
            box.prop(settings, "loc_offset", text="")
            row = box.row(align=True)
            row.operator("liveoffset.copy_location", text="Copy", icon='COPYDOWN')
            row.operator("liveoffset.paste_location", text="Paste", icon='PASTEDOWN')

            # ── Rotation Offset ──────────────────────────────────────────────
            box = layout.box()
            box.enabled = not transforming
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
            row = box.row(align=True)
            row.operator("liveoffset.copy_rotation", text="Copy", icon='COPYDOWN')
            row.operator("liveoffset.paste_rotation", text="Paste", icon='PASTEDOWN')

            # ── Scale Offset ─────────────────────────────────────────────────
            box = layout.box()
            box.label(text="Scale Offset")
            box.enabled = not transforming
            box.prop(settings, "scale_offset", text="")
            row = box.row(align=True)
            row.operator("liveoffset.copy_scale", text="Copy", icon='COPYDOWN')
            row.operator("liveoffset.paste_scale", text="Paste", icon='PASTEDOWN')

        layout.separator()
        row = layout.row(align=True)
        row.enabled = not transforming
        row.operator("liveoffset.copy", text="Copy All Offsets", icon='COPYDOWN')
        row.operator("liveoffset.paste", text="Paste All Offsets", icon='PASTEDOWN')

        col = layout.column(align=True)
        col.scale_y = 1.3
        col.enabled = not transforming
        col.operator("liveoffset.apply", text="Apply to All Keyframes", icon='CHECKMARK')
        col.operator("liveoffset.apply_selected", text="Apply to Selected Keyframes", icon='RESTRICT_SELECT_OFF')

        row = layout.row(align=True)
        row.enabled = not transforming
        row.operator("liveoffset.reset", text="Reset", icon='LOOP_BACK')
        row.operator("liveoffset.refresh", text="", icon='FILE_REFRESH')


class TRANSFORMSHORTCUTS_PT_panel(Panel):
    bl_label = "Transform Shortcuts"
    bl_idname = "TRANSFORMSHORTCUTS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Transform Shortcuts"

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

        bone = context.active_pose_bone
        if not bone:
            layout.label(text="Select a bone", icon='INFO')
            return

        transforming = _is_transform_running()
        settings = wm.transform_shortcuts_settings

        box = layout.box()
        box.label(text=f"Active: {bone.name}", icon='BONE_DATA')
        box.label(text=f"Frame: {context.scene.frame_current}", icon='TIME')
        if transforming:
            box.label(text="Transforming… (values frozen)", icon='TIME')

        row = layout.row()
        row.label(text="Rotation Mode")
        row.prop(bone, "rotation_mode", text="")

        # ── Location ─────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Location")
        box.enabled = not transforming
        box.prop(settings, "location", text="")
        row = box.row(align=True)
        row.operator("transformshortcuts.copy_location", text="Copy", icon='COPYDOWN')
        row.operator("transformshortcuts.paste_location", text="Paste", icon='PASTEDOWN')

        # ── Rotation ─────────────────────────────────────────────────────────
        box = layout.box()
        box.enabled = not transforming
        if bone.rotation_mode == 'QUATERNION':
            box.label(text="Rotation (Quaternion)")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=0, text="W")
            row.prop(settings, "rotation", index=1, text="X")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=2, text="Y")
            row.prop(settings, "rotation", index=3, text="Z")
        elif bone.rotation_mode == 'AXIS_ANGLE':
            box.label(text="Rotation (Axis-Angle)")
            box.prop(settings, "rotation", index=0, text="Angle")
            row = box.row(align=True)
            row.prop(settings, "rotation", index=1, text="X")
            row.prop(settings, "rotation", index=2, text="Y")
            row.prop(settings, "rotation", index=3, text="Z")
        else:
            box.label(text="Rotation (Euler)")
            box.prop(settings, "rotation", index=0, text="X")
            box.prop(settings, "rotation", index=1, text="Y")
            box.prop(settings, "rotation", index=2, text="Z")
        row = box.row(align=True)
        row.operator("transformshortcuts.copy_rotation", text="Copy", icon='COPYDOWN')
        row.operator("transformshortcuts.paste_rotation", text="Paste", icon='PASTEDOWN')

        # ── Scale ────────────────────────────────────────────────────────────
        box = layout.box()
        box.label(text="Scale")
        box.enabled = not transforming
        box.prop(settings, "scale", text="")
        row = box.row(align=True)
        row.operator("transformshortcuts.copy_scale", text="Copy", icon='COPYDOWN')
        row.operator("transformshortcuts.paste_scale", text="Paste", icon='PASTEDOWN')

        layout.separator()
        row = layout.row(align=True)
        row.scale_y = 1.4
        row.enabled = not transforming
        row.operator("transformshortcuts.copy", text="Copy All", icon='COPYDOWN')
        row.operator("transformshortcuts.paste", text="Paste All", icon='PASTEDOWN')
        row = layout.row()
        row.enabled = not transforming
        row.operator("transformshortcuts.refresh", text="Refresh", icon='FILE_REFRESH')


class ANIMSHORTCUTS_PT_panel(Panel):
    bl_label = "Anim Shortcuts"
    bl_idname = "ANIMSHORTCUTS_PT_panel"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Anim Shortcuts"

    def draw(self, context):
        layout = self.layout
        obj = context.active_object
        settings = context.window_manager.anim_shortcuts_settings

        if not obj or obj.type != 'ARMATURE':
            layout.label(text="Select an Armature", icon='ERROR')
            return
        if obj.mode != 'POSE':
            layout.label(text="Switch to Pose Mode", icon='ERROR')
            return
        if not obj.animation_data or not obj.animation_data.action:
            layout.label(text="No active Action", icon='ERROR')
            return

        # Auto-suggest half length when the preferred value is still the default
        # and we have a sensible action length
        total = get_action_total_length(context)
        if total >= 16 and settings.prefer_period == 30:
            # only auto-set once (when still at factory default)
            settings.prefer_period = max(8, int(round(total / 2)))

        box = layout.box()
        box.label(text="Loop Detection (Walk Cycle)", icon='LOOP_FORWARDS')

        col = box.column(align=True)
        col.prop(settings, "prefer_period")
        row = col.row(align=True)
        row.operator("animshortcuts.set_prefer_half", text="Set to Half Length", icon='ARROW_LEFTRIGHT')
        if total > 0:
            row = col.row()
            row.scale_y = 0.8
            row.label(text=f"Action length ≈ {total} frames", icon='TIME')

        col.separator()
        col.prop(settings, "loop_tolerance", slider=True)
        col.prop(settings, "compare_mode")
        col.prop(settings, "ignore_root_location")

        layout.separator()

        col = layout.column(align=True)
        col.scale_y = 1.4
        col.operator("animshortcuts.detect_loop", text="Detect Loop", icon='TIME')
        col.operator("animshortcuts.detect_truncate_loop", text="Detect and Truncate Loop", icon='TRASH')
        col.operator("animshortcuts.homogenise", text="Homogenise Animation", icon='MOD_SMOOTH')

        layout.separator()
        info = layout.column(align=True)
        info.scale_y = 0.8
        info.label(text="Prefer Period ≈ length of ONE walk cycle", icon='INFO')
        info.label(text="Homogenise forces later cycles to match the first", icon='INFO')

        # ── Copy Keyframes to another Action ────────────────────────────────
        layout.separator()
        box = layout.box()
        box.label(text="Copy Keyframes to Action", icon='ACTION')
        box.prop(settings, "target_action", text="")
        row = box.row()
        row.scale_y = 1.3
        row.operator(
            "animshortcuts.copy_bones_to_action",
            text="Copy Selected Bones Keyframes",
            icon='COPYDOWN',
        )
        box.label(
            text="Copies all keys of selected bones from the active Action",
            icon='INFO',
        )


# ─────────────────────────────────────────────────────────────────────────────
# Registration
# ─────────────────────────────────────────────────────────────────────────────

classes = (
    LIVEOFFSET_PG_settings,
    TRANSFORMSHORTCUTS_PG_settings,
    ANIMSHORTCUTS_PG_settings,
    LIVEOFFSET_OT_apply,
    LIVEOFFSET_OT_apply_selected,
    LIVEOFFSET_OT_refresh,
    LIVEOFFSET_OT_reset,
    LIVEOFFSET_OT_copy,
    LIVEOFFSET_OT_paste,
    LIVEOFFSET_OT_copy_location,
    LIVEOFFSET_OT_paste_location,
    LIVEOFFSET_OT_copy_rotation,
    LIVEOFFSET_OT_paste_rotation,
    LIVEOFFSET_OT_copy_scale,
    LIVEOFFSET_OT_paste_scale,
    TRANSFORMSHORTCUTS_OT_copy,
    TRANSFORMSHORTCUTS_OT_paste,
    TRANSFORMSHORTCUTS_OT_refresh,
    TRANSFORMSHORTCUTS_OT_copy_location,
    TRANSFORMSHORTCUTS_OT_paste_location,
    TRANSFORMSHORTCUTS_OT_copy_rotation,
    TRANSFORMSHORTCUTS_OT_paste_rotation,
    TRANSFORMSHORTCUTS_OT_copy_scale,
    TRANSFORMSHORTCUTS_OT_paste_scale,
    ANIMSHORTCUTS_OT_set_prefer_half,
    ANIMSHORTCUTS_OT_detect_loop,
    ANIMSHORTCUTS_OT_detect_truncate_loop,
    ANIMSHORTCUTS_OT_homogenise,
    ANIMSHORTCUTS_OT_copy_bones_to_action,
    LIVEOFFSET_PT_panel,
    TRANSFORMSHORTCUTS_PT_panel,
    ANIMSHORTCUTS_PT_panel,
)


def register():
    for cls in classes:
        try:
            bpy.utils.register_class(cls)
        except ValueError:
            bpy.utils.unregister_class(cls)
            bpy.utils.register_class(cls)

    if not hasattr(bpy.types.WindowManager, "live_offset_settings"):
        bpy.types.WindowManager.live_offset_settings = bpy.props.PointerProperty(type=LIVEOFFSET_PG_settings)
    if not hasattr(bpy.types.WindowManager, "transform_shortcuts_settings"):
        bpy.types.WindowManager.transform_shortcuts_settings = bpy.props.PointerProperty(type=TRANSFORMSHORTCUTS_PG_settings)
    if not hasattr(bpy.types.WindowManager, "anim_shortcuts_settings"):
        bpy.types.WindowManager.anim_shortcuts_settings = bpy.props.PointerProperty(type=ANIMSHORTCUTS_PG_settings)

    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)


def unregister():
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)

    for attr in ("live_offset_settings", "transform_shortcuts_settings", "anim_shortcuts_settings"):
        if hasattr(bpy.types.WindowManager, attr):
            delattr(bpy.types.WindowManager, attr)

    for cls in reversed(classes):
        try:
            bpy.utils.unregister_class(cls)
        except RuntimeError:
            pass


if __name__ == "__main__":
    register()