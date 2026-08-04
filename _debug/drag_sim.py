import bpy

import sys
sys.path.insert(0, r"c:\repos\blender-transform-propagate")
import live_transform_offset as lto

lto.register()

bpy.ops.object.armature_add()
obj = bpy.context.active_object
bpy.ops.object.mode_set(mode='POSE')
bone = obj.pose.bones[0]
bone.rotation_mode = 'QUATERNION'

obj.animation_data_create()
action = bpy.data.actions.new("TestAction")
obj.animation_data.action = action

scene = bpy.context.scene
scene.frame_set(1)
bone.keyframe_insert(data_path="location", frame=1)
bone.keyframe_insert(data_path="rotation_quaternion", frame=1)
bone.keyframe_insert(data_path="scale", frame=1)

scene.frame_set(10)
bpy.ops.pose.select_all(action='SELECT')
bpy.context.view_layer.update()

with bpy.context.temp_override(active_object=obj, selected_pose_bones=[bone], active_pose_bone=bone):
    ctx = bpy.context
    wm = ctx.window_manager

    print("=== simulating 5 'gizmo drag' ticks ===")
    for step in range(5):
        target = 0.1 * (step + 1)
        bone.location[0] = target  # simulate the transform operator moving the bone
        print(f"tick {step}: set bone.location[0] = {target}")

        # simulate what the depsgraph handler + panel draw() do on every redraw
        lto._depsgraph_update(scene, None)
        lto.sync_offsets_from_bone(ctx)
        pending = lto.collect_offsets(ctx)

        print(f"  after sync+collect: bone.location[0] = {bone.location[0]}  (expected {target})")
        settings = wm.live_offset_settings
        print(f"  settings.loc_offset = {list(settings.loc_offset)}")
        if abs(bone.location[0] - target) > 1e-9:
            print("  !!! MISMATCH – something reset the bone location !!!")
