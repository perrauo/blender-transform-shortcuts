import json
import math

import bpy


IK_FK_PROPERTY = 'IK_FK'
CONSTRAINT_TAG = 'UE_DIRECT_RETARGET'
HELPER_COLLECTION = 'UE_DIRECT_RETARGET_HELPERS'
HELPER_PREFIX = 'UE_DIRECT_RETARGET_OFFSET'

COPY_ROTATION = 'COPY_ROTATION'
COPY_LOCATION = 'COPY_LOCATION'
COPY_TRANSFORMS = 'COPY_TRANSFORMS'
ROOT = 'ROOT'
SPINE_FK_PREFIX = 'spine_fk'
TWEAK_SPINE_PREFIX = 'tweak_spine'
TORSO_CONTROLS = {'hips', 'torso', 'chest'}
FINGER_CONTROL_PREFIXES = (
    'thumb.',
    'f_index.',
    'f_middle.',
    'f_ring.',
    'f_pinky.',
)

LIMB_FK_CONTROL_PREFIXES = (
    'upper_arm_fk.',
    'forearm_fk.',
    'hand_fk.',
    'thigh_fk.',
    'shin_fk.',
    'foot_fk.',
)
LIMB_TWEAK_CONTROL_PREFIXES = (
    'upper_arm_tweak.',
    'forearm_tweak.',
    'hand_tweak.',
    'thigh_tweak.',
    'shin_tweak.',
    'foot_tweak.',
)
IK_END_CONTROLS = {
    'hand_ik.L',
    'hand_ik.R',
    'foot_ik.L',
    'foot_ik.R',
}


def _side_pairs(left_source, right_source, left_control, right_control, constraint_type):
    return [
        {'source': left_source, 'control': left_control, 'type': constraint_type},
        {'source': right_source, 'control': right_control, 'type': constraint_type},
    ]


def _finger_pairs(finger_name, control_name):
    mappings = []
    for source_side, control_side in [('l', 'L'), ('r', 'R')]:
        for index in ('01', '02', '03'):
            mappings.append({
                'source': f'{finger_name}_{index}_{source_side}',
                'control': f'{control_name}.{index}.{control_side}',
                'type': COPY_ROTATION,
            })
    return mappings


def _common_finger_mappings():
    mappings = []
    mappings.extend(_finger_pairs('thumb', 'thumb'))
    mappings.extend(_finger_pairs('index', 'f_index'))
    mappings.extend(_finger_pairs('middle', 'f_middle'))
    mappings.extend(_finger_pairs('ring', 'f_ring'))
    mappings.extend(_finger_pairs('pinky', 'f_pinky'))
    return mappings


def _mixamo_finger_mappings():
    """Return standard Mixamo finger links, including its 2-based names."""
    mappings = []
    finger_pairs = (
        ('Thumb', 'thumb'),
        ('Index', 'f_index'),
        ('Middle', 'f_middle'),
        ('Ring', 'f_ring'),
        ('Pinky', 'f_pinky'),
    )
    segment_pairs = (('2', '01'), ('3', '02'), ('4', '03'))
    for source_side, control_side in (('Left', 'L'), ('Right', 'R')):
        for source_finger, control_finger in finger_pairs:
            for source_segment, control_segment in segment_pairs:
                mappings.append({
                    'source': (
                        f'mixamorig:{source_side}Hand'
                        f'{source_finger}{source_segment}'
                    ),
                    'control': (
                        f'{control_finger}.{control_segment}.{control_side}'
                    ),
                    'type': COPY_ROTATION,
                })
    return mappings


def _tweak_mappings():
    """Return UE mannequin secondary-control links from the official template."""
    mappings = []
    for source_side, control_side in [('l', 'L'), ('r', 'R')]:
        mappings.extend([
            {'source': f'upperarm_correctiveRoot_{source_side}', 'control': f'upper_arm_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'upperarm_twist_01_{source_side}', 'control': f'upper_arm_tweak.{control_side}.001', 'type': COPY_TRANSFORMS},
            {'source': f'upperarm_twist_02_{source_side}', 'control': f'upper_arm_tweak.{control_side}.002', 'type': COPY_TRANSFORMS},
            {'source': f'lowerarm_correctiveRoot_{source_side}', 'control': f'forearm_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'lowerarm_twist_02_{source_side}', 'control': f'forearm_tweak.{control_side}.001', 'type': COPY_TRANSFORMS},
            {'source': f'lowerarm_twist_01_{source_side}', 'control': f'forearm_tweak.{control_side}.002', 'type': COPY_TRANSFORMS},
            {'source': f'hand_{source_side}', 'control': f'hand_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'thigh_correctiveRoot_{source_side}', 'control': f'thigh_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'thigh_twist_01_{source_side}', 'control': f'thigh_tweak.{control_side}.001', 'type': COPY_TRANSFORMS},
            {'source': f'thigh_twist_02_{source_side}', 'control': f'thigh_tweak.{control_side}.002', 'type': COPY_TRANSFORMS},
            {'source': f'calf_correctiveRoot_{source_side}', 'control': f'shin_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'calf_twist_02_{source_side}', 'control': f'shin_tweak.{control_side}.001', 'type': COPY_TRANSFORMS},
            {'source': f'calf_twist_01_{source_side}', 'control': f'shin_tweak.{control_side}.002', 'type': COPY_TRANSFORMS},
            {'source': f'foot_{source_side}', 'control': f'foot_tweak.{control_side}', 'type': COPY_TRANSFORMS},
            {'source': f'clavicle_pec_{source_side}', 'control': f'breast.{control_side}', 'type': COPY_TRANSFORMS},
        ])
    return mappings


def _spine_tweak_mappings():
    """Return the lower-torso links used by the UE2Rigify templates.

    The first two spine tweaks establish the pelvis and lower-spine anchors
    used by Rigify's torso mechanism. They are distinct from the remaining
    automatically generated tweak controls, which must not be substituted for
    missing FK controls.
    """
    return [
        {'source': 'pelvis', 'control': TWEAK_SPINE_PREFIX, 'type': COPY_TRANSFORMS},
        {'source': 'spine_01', 'control': f'{TWEAK_SPINE_PREFIX}.001', 'type': COPY_TRANSFORMS},
    ]


def _ue4_spine_base_mappings():
    """Anchor the UE4 FK spine without animating its torso master controls.

    The direct retargeter intentionally leaves the ``hips``, ``torso``, and
    ``chest`` widgets untouched. Their world-space transforms are high-level
    Rigify mechanism controls; constraining them alongside the FK chain makes
    the upper body inherit a second pelvis translation. The base FK control is
    sufficient to establish the pelvis anchor for the animated spine chain.
    """
    return [
        {'source': 'pelvis', 'control': SPINE_FK_PREFIX, 'type': COPY_TRANSFORMS},
    ]


def _spine_fk_mappings(source_controls):
    """Map a spine chain with the rest-relative transforms UE2Rigify uses."""
    return [
        {
            'source': source_bone,
            'control': control_bone,
            'type': COPY_TRANSFORMS,
        }
        for source_bone, control_bone in source_controls
    ]


def _source_spine_bones(profile_id):
    """Return source spine segments ordered from pelvis to chest."""
    return {
        'UE4_MANNY': ('spine_01', 'spine_02', 'spine_03'),
        'UE5_MANNY': ('spine_01', 'spine_02', 'spine_03', 'spine_04', 'spine_05'),
        'UEFN': ('spine_01', 'spine_02', 'spine_03', 'spine_04', 'spine_05'),
        'MIXAMO': (
            'mixamorig:Spine',
            'mixamorig:Spine1',
            'mixamorig:Spine2',
        ),
    }[profile_id]


def _source_hip_bone(profile_id):
    """Return the source bone that represents the pelvis."""
    if profile_id == 'MIXAMO':
        return 'mixamorig:Hips'
    return 'pelvis'


def _source_fk_bones(profile_id):
    """Return source joints matching a generic Rigify FK spine in order."""
    return (_source_hip_bone(profile_id), *_source_spine_bones(profile_id))


def _torso_control_mappings(profile_id):
    """Represent the source spine with Rigify's three broad torso controls.

    ``torso`` carries pelvis translation for the whole chain. ``hips`` then
    carries the pelvis orientation without duplicating that translation, and
    ``chest`` carries the top spine transform relative to the translated torso.
    Rigify distributes the hips/chest deltas over its lower and upper spine,
    producing an editable approximation without individual spine FK keys.
    """
    hip_bone = _source_hip_bone(profile_id)
    mappings = [
        {
            'source': hip_bone,
            'control': 'torso',
            'type': COPY_LOCATION,
        },
        {
            'source': hip_bone,
            'control': 'hips',
            'type': COPY_TRANSFORMS,
        },
        {
            'source': _source_spine_bones(profile_id)[-1],
            'control': 'chest',
            'type': COPY_TRANSFORMS,
        },
    ]

    if profile_id == 'MIXAMO':
        # Mixamo's root mapping sends forward motion to Rigify's root. Keep
        # the other two axes on the torso and only rotation on the hips so
        # translation is not inherited twice.
        mappings[0]['use_y'] = False
        mappings[1]['type'] = COPY_ROTATION
    return mappings


def _ik_mappings():
    return [
        {'source': 'ik_hand_l', 'control': 'hand_ik.L', 'type': COPY_TRANSFORMS},
        {'source': 'ik_hand_r', 'control': 'hand_ik.R', 'type': COPY_TRANSFORMS},
        {'source': 'ik_foot_l', 'control': 'foot_ik.L', 'type': COPY_TRANSFORMS},
        {'source': 'ik_foot_r', 'control': 'foot_ik.R', 'type': COPY_TRANSFORMS},
    ]


def _ik_driven_mappings(profile_id):
    """Drive Rigify IK controls from the evaluated source limb pose.

    Deform hand and foot joints always describe the visible end-effector pose,
    so they are the reliable source for an IK-authored bake. Rest-relative
    lower-limb motion carries Rigify's pole controls with the source elbow and
    knee plane.
    """
    mappings = []
    if profile_id == 'MIXAMO':
        mappings.extend(_side_pairs(
            'mixamorig:LeftHand',
            'mixamorig:RightHand',
            'hand_ik.L',
            'hand_ik.R',
            COPY_TRANSFORMS,
        ))
        mappings.extend(_side_pairs(
            'mixamorig:LeftFoot',
            'mixamorig:RightFoot',
            'foot_ik.L',
            'foot_ik.R',
            COPY_TRANSFORMS,
        ))
        mappings.extend(_side_pairs(
            'mixamorig:LeftForeArm',
            'mixamorig:RightForeArm',
            'upper_arm_ik_target.L',
            'upper_arm_ik_target.R',
            COPY_LOCATION,
        ))
        mappings.extend(_side_pairs(
            'mixamorig:LeftLeg',
            'mixamorig:RightLeg',
            'thigh_ik_target.L',
            'thigh_ik_target.R',
            COPY_LOCATION,
        ))
        return mappings

    mappings.extend(_side_pairs(
        'hand_l', 'hand_r', 'hand_ik.L', 'hand_ik.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs(
        'foot_l', 'foot_r', 'foot_ik.L', 'foot_ik.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs(
        'lowerarm_l',
        'lowerarm_r',
        'upper_arm_ik_target.L',
        'upper_arm_ik_target.R',
        COPY_LOCATION,
    ))
    mappings.extend(_side_pairs(
        'calf_l',
        'calf_r',
        'thigh_ik_target.L',
        'thigh_ik_target.R',
        COPY_LOCATION,
    ))
    return mappings


def _is_limb_fk_or_tweak_control(control_name):
    return control_name.startswith(
        LIMB_FK_CONTROL_PREFIXES + LIMB_TWEAK_CONTROL_PREFIXES)


def _is_spine_fk_or_tweak_control(control_name):
    return (
        control_name == SPINE_FK_PREFIX
        or control_name.startswith(f'{SPINE_FK_PREFIX}.')
        or control_name == TWEAK_SPINE_PREFIX
        or control_name.startswith(f'{TWEAK_SPINE_PREFIX}.')
    )


def _is_finger_control(control_name):
    return control_name.startswith(FINGER_CONTROL_PREFIXES)


def _apply_torso_control_mode(scene, mappings):
    if not getattr(scene, 'udr_torso_controls', False):
        return mappings

    mappings = [
        mapping
        for mapping in mappings
        if not _is_spine_fk_or_tweak_control(mapping['control'])
        and mapping['control'] not in TORSO_CONTROLS
    ]
    return [*mappings, *_torso_control_mappings(scene.udr_profile)]


def _apply_finger_mode(scene, mappings):
    if getattr(scene, 'udr_bake_fingers', True):
        return mappings
    return [
        mapping
        for mapping in mappings
        if not _is_finger_control(mapping['control'])
    ]


def _validate_authoring_controls(scene, source_rig, rigify_rig):
    if not getattr(scene, 'udr_torso_controls', False):
        return

    missing_controls = sorted(
        control_name
        for control_name in TORSO_CONTROLS
        if not rigify_rig.pose.bones.get(control_name)
    )
    if missing_controls:
        raise ValueError(
            'High-Level Torso Controls requires target controls: '
            f'{", ".join(missing_controls)}.'
        )

    source_bones = {
        _source_hip_bone(scene.udr_profile),
        _source_spine_bones(scene.udr_profile)[-1],
    }
    missing_source_bones = sorted(
        bone_name
        for bone_name in source_bones
        if not _resolve_source_bone_name(source_rig, bone_name)
    )
    if missing_source_bones:
        raise ValueError(
            'High-Level Torso Controls requires source bones: '
            f'{", ".join(missing_source_bones)}.'
        )


def _apply_drive_mode(scene, mappings):
    if not getattr(scene, 'udr_ik_driven', False):
        return mappings

    # An IK solve and direct FK/tweak transforms must not both drive the same
    # limb. Keep shoulders, toes, fingers, and torso controls, but replace the
    # limb FK/tweak links and optional Unreal IK-bone links with a coherent IK
    # end-effector and pole-control set.
    mappings = [
        mapping
        for mapping in mappings
        if mapping['control'] not in IK_END_CONTROLS
        and not _is_limb_fk_or_tweak_control(mapping['control'])
    ]
    return [*mappings, *_ik_driven_mappings(scene.udr_profile)]


def _ue4_profile():
    mappings = [
        {'source': 'object', 'control': 'root', 'type': ROOT},
        {'source': 'neck_01', 'control': 'neck', 'type': COPY_ROTATION},
        {'source': 'head', 'control': 'head', 'type': COPY_ROTATION},
    ]
    mappings.extend(_spine_fk_mappings([
        ('spine_01', 'spine_fk.001'),
        ('spine_02', 'spine_fk.002'),
        ('spine_03', 'spine_fk.003'),
    ]))
    mappings.extend(_side_pairs('clavicle_l', 'clavicle_r', 'shoulder.L', 'shoulder.R', COPY_ROTATION))
    mappings.extend(_side_pairs('upperarm_l', 'upperarm_r', 'upper_arm_fk.L', 'upper_arm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('lowerarm_l', 'lowerarm_r', 'forearm_fk.L', 'forearm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('hand_l', 'hand_r', 'hand_fk.L', 'hand_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('thigh_l', 'thigh_r', 'thigh_fk.L', 'thigh_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('calf_l', 'calf_r', 'shin_fk.L', 'shin_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('foot_l', 'foot_r', 'foot_fk.L', 'foot_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('ball_l', 'ball_r', 'toe.L', 'toe.R', COPY_TRANSFORMS))
    mappings.extend(_ue4_spine_base_mappings())
    mappings.extend(_spine_tweak_mappings())
    mappings.extend(_tweak_mappings())
    mappings.extend(_common_finger_mappings())
    mappings.extend(_ik_mappings())
    return mappings


def _ue5_profile():
    mappings = [
        {'source': 'object', 'control': 'root', 'type': ROOT},
        # These controls have a parent-space offset above spine_fk.008. Copy
        # their complete rest-relative transform, as the UE5 UE2Rigify
        # template does, so their world-space position stays on the spine.
        {'source': 'neck_02', 'control': 'neck', 'type': COPY_TRANSFORMS},
        {'source': 'head', 'control': 'head', 'type': COPY_TRANSFORMS},
    ]
    mappings.extend(_spine_fk_mappings([
        ('spine_01', 'spine_fk'),
        ('spine_02', 'spine_fk.001'),
        # The UE5 template has an extra FK control at the spine_02 joint.
        ('spine_02', 'spine_fk.002'),
        ('spine_03', 'spine_fk.003'),
        ('spine_04', 'spine_fk.007'),
        ('spine_05', 'spine_fk.008'),
    ]))
    mappings.extend(_side_pairs('clavicle_l', 'clavicle_r', 'shoulder.L', 'shoulder.R', COPY_ROTATION))
    mappings.extend(_side_pairs('upperarm_l', 'upperarm_r', 'upper_arm_fk.L', 'upper_arm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('lowerarm_l', 'lowerarm_r', 'forearm_fk.L', 'forearm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('hand_l', 'hand_r', 'hand_fk.L', 'hand_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('thigh_l', 'thigh_r', 'thigh_fk.L', 'thigh_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('calf_l', 'calf_r', 'shin_fk.L', 'shin_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('foot_l', 'foot_r', 'foot_fk.L', 'foot_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('ball_l', 'ball_r', 'toe.L', 'toe.R', COPY_TRANSFORMS))
    mappings.extend(_spine_tweak_mappings())
    mappings.extend(_tweak_mappings())
    mappings.extend(_common_finger_mappings())
    mappings.extend(_ik_mappings())
    return mappings


def _uefn_profile():
    mappings = [
        {'source': 'object', 'control': 'root', 'type': ROOT},
        {'source': 'neck_02', 'control': 'neck', 'type': COPY_ROTATION},
        {'source': 'head', 'control': 'head', 'type': COPY_ROTATION},
    ]
    mappings.extend(_spine_fk_mappings([
        ('spine_01', 'spine_fk'),
        ('spine_02', 'spine_fk.001'),
        # The UEFN template uses the same expanded UE5 spine control layout.
        ('spine_02', 'spine_fk.002'),
        ('spine_03', 'spine_fk.003'),
        ('spine_04', 'spine_fk.007'),
        ('spine_05', 'spine_fk.008'),
    ]))
    mappings.extend(_side_pairs('clavicle_l', 'clavicle_r', 'shoulder.L', 'shoulder.R', COPY_ROTATION))
    mappings.extend(_side_pairs('upperarm_l', 'upperarm_r', 'upper_arm_fk.L', 'upper_arm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('lowerarm_l', 'lowerarm_r', 'forearm_fk.L', 'forearm_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('hand_l', 'hand_r', 'hand_fk.L', 'hand_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('thigh_l', 'thigh_r', 'thigh_fk.L', 'thigh_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('calf_l', 'calf_r', 'shin_fk.L', 'shin_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('foot_l', 'foot_r', 'foot_fk.L', 'foot_fk.R', COPY_TRANSFORMS))
    mappings.extend(_side_pairs('ball_l', 'ball_r', 'toe.L', 'toe.R', COPY_TRANSFORMS))
    mappings.extend(_spine_tweak_mappings())
    mappings.extend(_tweak_mappings())
    mappings.extend(_common_finger_mappings())
    mappings.extend(_ik_mappings())
    return mappings


def _mixamo_profile():
    """Return standard namespaced Mixamo-to-Rigify control mappings."""
    hips = 'mixamorig:Hips'
    mappings = [
        # Mixamo stores locomotion and pelvic motion together on Hips. Match
        # the established Mixaify decomposition: forward motion goes to the
        # Rigify root while lateral/vertical motion and pelvic rotation stay
        # on the torso mechanism.
        {
            'source': hips,
            'control': 'root',
            'type': COPY_LOCATION,
            'use_x': False,
            'use_z': False,
        },
        {
            'source': hips,
            'control': 'torso',
            'type': COPY_LOCATION,
            'use_y': False,
        },
        {
            'source': hips,
            'control': 'torso',
            'type': COPY_ROTATION,
            'use_x': False,
            'use_z': False,
        },
        {
            'source': 'mixamorig:Spine1',
            'control': 'spine_fk.002',
            'type': COPY_TRANSFORMS,
        },
        {
            'source': 'mixamorig:Spine2',
            'control': 'spine_fk.003',
            'type': COPY_ROTATION,
        },
        {'source': 'mixamorig:Neck', 'control': 'neck', 'type': COPY_ROTATION},
        {'source': 'mixamorig:Head', 'control': 'head', 'type': COPY_ROTATION},
    ]
    mappings.extend(_side_pairs(
        'mixamorig:LeftShoulder',
        'mixamorig:RightShoulder',
        'shoulder.L',
        'shoulder.R',
        COPY_ROTATION,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftArm',
        'mixamorig:RightArm',
        'upper_arm_fk.L',
        'upper_arm_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftForeArm',
        'mixamorig:RightForeArm',
        'forearm_fk.L',
        'forearm_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftHand',
        'mixamorig:RightHand',
        'hand_fk.L',
        'hand_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftUpLeg',
        'mixamorig:RightUpLeg',
        'thigh_fk.L',
        'thigh_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftLeg',
        'mixamorig:RightLeg',
        'shin_fk.L',
        'shin_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend(_side_pairs(
        'mixamorig:LeftFoot',
        'mixamorig:RightFoot',
        'foot_fk.L',
        'foot_fk.R',
        COPY_TRANSFORMS,
    ))
    mappings.extend([
        {
            'source': 'mixamorig:LeftToeBase',
            'control': 'toe.L',
            'control_fallbacks': ('toe_fk.L',),
            'type': COPY_TRANSFORMS,
        },
        {
            'source': 'mixamorig:RightToeBase',
            'control': 'toe.R',
            'control_fallbacks': ('toe_fk.R',),
            'type': COPY_TRANSFORMS,
        },
    ])
    mappings.extend(_mixamo_finger_mappings())
    return mappings


PROFILE_DEFINITIONS = {
    'UE4_MANNY': {
        'label': 'UE4 Manny',
        'mappings': _ue4_profile(),
    },
    'UE5_MANNY': {
        'label': 'UE5 Manny / Quinn',
        'mappings': _ue5_profile(),
    },
    'UEFN': {
        'label': 'UEFN Mannequin',
        'mappings': _uefn_profile(),
    },
    'MIXAMO': {
        'label': 'Mixamo',
        'mappings': _mixamo_profile(),
    },
}

PROFILE_ITEMS = [
    (profile_id, profile_data['label'], profile_data['label'])
    for profile_id, profile_data in PROFILE_DEFINITIONS.items()
]

INVERT_Z_CONTROLS = {
    'thumb.02.L',
    'thumb.03.L',
    'thumb.02.R',
    'thumb.03.R',
}
def armature_poll(self, obj):
    return obj and obj.type == 'ARMATURE'


def action_poll(self, obj):
    return True


def source_rig_update(self, context):
    source_rig = self.udr_source_rig
    if source_rig and source_rig.animation_data and source_rig.animation_data.action:
        context.scene.udr_source_action = source_rig.animation_data.action


def rigify_rig_update(self, context):
    rigify_rig = self.udr_rigify_rig
    if rigify_rig and rigify_rig.animation_data and rigify_rig.animation_data.action:
        context.scene.udr_rigify_action = rigify_rig.animation_data.action


def retarget_settings_update(self, context):
    if not getattr(self, 'udr_enabled', False):
        return
    try:
        add_constraints(self)
    except Exception as error:
        print(f'Direct Retarget: could not update retarget settings: {error}')


def get_profile(scene):
    return PROFILE_DEFINITIONS[scene.udr_profile]


def _resolve_source_bone_name(source_rig, requested_name):
    """Resolve a source bone across namespace export variations."""
    if requested_name == 'object':
        return requested_name
    if source_rig.pose.bones.get(requested_name):
        return requested_name

    requested_basename = requested_name.rsplit(':', 1)[-1]
    matches = [
        pose_bone.name
        for pose_bone in source_rig.pose.bones
        if pose_bone.name.rsplit(':', 1)[-1] == requested_basename
    ]
    if len(matches) == 1:
        return matches[0]
    return None


def _resolve_target_control_mappings(mappings, rigify_rig):
    """Replace unavailable target names with profile-provided alternatives."""
    if not rigify_rig:
        return mappings

    resolved_mappings = []
    for mapping in mappings:
        if rigify_rig.pose.bones.get(mapping['control']):
            resolved_mappings.append(mapping)
            continue

        fallback = next((
            control_name
            for control_name in mapping.get('control_fallbacks', ())
            if rigify_rig.pose.bones.get(control_name)
        ), None)
        if fallback:
            mapping = {**mapping, 'control': fallback}
        resolved_mappings.append(mapping)
    return resolved_mappings


def _profile_spine_fk_controls(mappings):
    return [
        mapping['control']
        for mapping in mappings
        if mapping['control'] == SPINE_FK_PREFIX
        or mapping['control'].startswith(f'{SPINE_FK_PREFIX}.')
    ]


def _available_spine_fk_controls(rigify_rig):
    """Return the target's primary FK spine controls from pelvis to chest.

    The first two ``tweak_spine`` controls have their own template mappings,
    but they are not a replacement FK chain when the target has fewer
    ``spine_fk`` controls.
    """
    control_names = [
        pose_bone.name
        for pose_bone in rigify_rig.pose.bones
        if pose_bone.name == SPINE_FK_PREFIX
        or pose_bone.name.startswith(f'{SPINE_FK_PREFIX}.')
    ]

    def spine_index(control_name):
        if control_name == SPINE_FK_PREFIX:
            return 0
        return int(control_name.rsplit('.', 1)[1]) + 1

    return sorted(control_names, key=spine_index)


def _has_profile_spine_fk_controls(rigify_rig, mappings):
    spine_controls = _profile_spine_fk_controls(mappings)
    return bool(spine_controls) and all(
        rigify_rig.pose.bones.get(control_name)
        for control_name in spine_controls
    )


def _partial_spine_fk_mappings(profile_id, rigify_rig):
    """Map a shorter generic Rigify spine by matching its joint topology.

    A standard four-control Rigify spine represents pelvis through spine_03,
    just like the working UE4 profile. Spreading UE5's five spine bones over
    those controls maps the target chest directly to spine_05. Because these
    are world-space transform links, that also copies translation accumulated
    across the omitted spine_04/spine_05 joints and makes the chest slide
    independently from its shorter target chain. Keep the available controls
    consecutive instead; the rotation-only neck link carries the final UE5
    upper-chain orientation without disconnecting its location from the chest.
    """
    if profile_id == 'MIXAMO':
        # The Mixamo profile deliberately decomposes Hips motion between
        # root and torso. Adding Hips to a fallback FK spine control would
        # drive the same pelvis motion twice through the target hierarchy.
        return []

    control_bones = _available_spine_fk_controls(rigify_rig)
    source_bones = _source_fk_bones(profile_id)
    return _spine_fk_mappings(zip(source_bones, control_bones))


def get_profile_mappings(scene, rigify_rig=None):
    """Get mappings compatible with the selected target Rigify spine layout."""
    mappings = get_profile(scene)['mappings']
    if not rigify_rig or _has_profile_spine_fk_controls(rigify_rig, mappings):
        compatible_mappings = mappings
    else:
        partial_spine_mappings = _partial_spine_fk_mappings(
            scene.udr_profile, rigify_rig)
        if not partial_spine_mappings:
            compatible_mappings = mappings
        else:
            # A standard Rigify human may have fewer primary spine controls
            # than the UE templates. Map its available FK chain in order while
            # retaining the two lower-torso tweak links used by the reference
            # templates.
            non_spine_mappings = []
            for mapping in mappings:
                control_name = mapping['control']
                if (
                    control_name == SPINE_FK_PREFIX
                    or control_name.startswith(f'{SPINE_FK_PREFIX}.')
                ):
                    continue

                # The expanded UE5 template can copy the neck and head
                # translations because its rest chain and separate upper-spine
                # controls match the mannequin. A generic Rigify human has one
                # neck control in the same role as UE4 neck_01. Driving it from
                # UE5 neck_02 folds the second neck joint's independent motion
                # into that base control, making the neck/head sway separately
                # from the shorter chest chain. Use neck_01 for that control
                # and let the head's world rotation retain the total orientation
                # through neck_02. Both locations remain connected to the
                # target chain so its super_head mechanism does not stretch to
                # catch independently translated endpoints.
                if (
                    scene.udr_profile == 'UE5_MANNY'
                    and control_name in {'neck', 'head'}
                ):
                    mapping = {
                        **mapping,
                        'source': (
                            'neck_01'
                            if control_name == 'neck'
                            else mapping['source']
                        ),
                        'type': COPY_ROTATION,
                    }
                non_spine_mappings.append(mapping)

            compatible_mappings = [
                *non_spine_mappings,
                *partial_spine_mappings,
            ]

    compatible_mappings = _apply_torso_control_mode(
        scene, compatible_mappings)
    compatible_mappings = _apply_drive_mode(scene, compatible_mappings)
    compatible_mappings = _apply_finger_mode(scene, compatible_mappings)
    return _resolve_target_control_mappings(
        compatible_mappings, rigify_rig)


def build_links_data(scene):
    links_data = []
    for mapping in get_profile_mappings(scene, scene.udr_rigify_rig):
        links_data.append({
            'from_node': 'Source Rig',
            'to_node': 'Control Rig',
            'from_socket': mapping['source'],
            'to_socket': mapping['control'],
        })
    return links_data


def ensure_pose_mode(armature_object):
    previous_object = bpy.context.view_layer.objects.active
    previous_mode = bpy.context.mode

    bpy.context.view_layer.objects.active = armature_object
    armature_object.select_set(True)

    if armature_object.mode != 'POSE':
        bpy.ops.object.mode_set(mode='POSE')

    return previous_object, previous_mode


def restore_mode(previous_object, previous_mode):
    try:
        bpy.ops.object.mode_set(mode=previous_mode)
    except Exception:
        pass
    finally:
        bpy.context.view_layer.objects.active = previous_object


def _save_ikfk_state(rigify_rig):
    state = {}
    for bone in rigify_rig.pose.bones:
        if isinstance(bone.get(IK_FK_PROPERTY), float):
            state[bone.name] = bone[IK_FK_PROPERTY]
    return state


def _restore_ikfk_state(rigify_rig, state_json):
    if not state_json:
        return

    state = json.loads(state_json)
    for bone_name, value in state.items():
        bone = rigify_rig.pose.bones.get(bone_name)
        if bone and isinstance(bone.get(IK_FK_PROPERTY), float):
            bone[IK_FK_PROPERTY] = value


def _set_drive_mode(rigify_rig, ik_driven):
    # Rigify's IK_FK convention is 0.0 for IK and 1.0 for FK.
    value = 0.0 if ik_driven else 1.0
    for bone in rigify_rig.pose.bones:
        if isinstance(bone.get(IK_FK_PROPERTY), float):
            bone[IK_FK_PROPERTY] = value


def _suspend_target_action_for_preview(scene, rigify_rig):
    """Prevent an existing bake from adding motion during live retargeting."""
    animation_data = rigify_rig.animation_data
    if not animation_data:
        return

    if scene.udr_preview_action is None:
        scene.udr_preview_action = animation_data.action
    animation_data.action = None


def _restore_target_action_after_preview(scene, rigify_rig):
    """Restore the action that was active before live retargeting began."""
    if rigify_rig.animation_data:
        rigify_rig.animation_data.action = scene.udr_preview_action
    scene.udr_preview_action = None


def _remove_tagged_constraints(constraints):
    for constraint in list(constraints):
        if constraint.name.startswith(CONSTRAINT_TAG):
            constraints.remove(constraint)


def _clear_existing_constraints(rigify_rig):
    _remove_tagged_constraints(rigify_rig.constraints)
    for pose_bone in rigify_rig.pose.bones:
        _remove_tagged_constraints(pose_bone.constraints)


def _get_or_create_helper_collection():
    helper_collection = bpy.data.collections.get(HELPER_COLLECTION)
    if not helper_collection:
        helper_collection = bpy.data.collections.new(HELPER_COLLECTION)
        bpy.context.scene.collection.children.link(helper_collection)

    helper_collection.hide_viewport = True
    helper_collection.hide_render = True
    return helper_collection


def _clear_offset_helpers():
    helper_collection = bpy.data.collections.get(HELPER_COLLECTION)
    if not helper_collection:
        return

    for helper_object in list(helper_collection.objects):
        bpy.data.objects.remove(helper_object, do_unlink=True)

    bpy.data.collections.remove(helper_collection)


def _create_offset_helpers(source_rig, rigify_rig, source_bone_name, control_bone_name):
    """Create an offset chain that maps a source socket's rest delta to a control."""
    control_data_bone = rigify_rig.data.bones.get(control_bone_name)
    if not control_data_bone:
        return None

    if source_bone_name == 'object':
        source_rest_matrix = source_rig.matrix_world.copy()
    else:
        source_data_bone = source_rig.data.bones.get(source_bone_name)
        if not source_data_bone:
            return None
        source_rest_matrix = source_rig.matrix_world @ source_data_bone.matrix_local

    helper_collection = _get_or_create_helper_collection()
    source_helper = bpy.data.objects.new(
        f'{HELPER_PREFIX}_SOURCE_{control_bone_name}', None)
    control_helper = bpy.data.objects.new(
        f'{HELPER_PREFIX}_CONTROL_{control_bone_name}', None)
    helper_collection.objects.link(source_helper)
    helper_collection.objects.link(control_helper)

    source_helper.empty_display_type = 'PLAIN_AXES'
    control_helper.empty_display_type = 'PLAIN_AXES'
    source_helper.matrix_world = source_rest_matrix
    control_helper.matrix_world = rigify_rig.matrix_world @ control_data_bone.matrix_local

    # Keeping this world matrix while parenting makes the control helper inherit
    # only the source bone's animation delta from its rest pose.
    control_helper.parent = source_helper
    control_helper.matrix_parent_inverse = source_helper.matrix_world.inverted()

    source_constraint = source_helper.constraints.new(COPY_TRANSFORMS)
    source_constraint.name = CONSTRAINT_TAG
    source_constraint.target = source_rig
    if source_bone_name != 'object':
        source_constraint.subtarget = source_bone_name

    return control_helper


def _set_constraint_axes(constraint, mapping):
    for axis_name in ('use_x', 'use_y', 'use_z'):
        if axis_name in mapping:
            setattr(constraint, axis_name, mapping[axis_name])


def _add_copy_rotation_constraint(target_object, rigify_bone, mapping):
    constraint = rigify_bone.constraints.new('COPY_ROTATION')
    constraint.name = CONSTRAINT_TAG
    constraint.target = target_object
    constraint.target_space = 'WORLD'
    constraint.owner_space = 'WORLD'
    _set_constraint_axes(constraint, mapping)
    if rigify_bone.name in INVERT_Z_CONTROLS:
        constraint.invert_z = True


def _add_copy_location_constraint(target_object, rigify_bone, mapping):
    constraint = rigify_bone.constraints.new('COPY_LOCATION')
    constraint.name = CONSTRAINT_TAG
    constraint.target = target_object
    constraint.target_space = 'WORLD'
    constraint.owner_space = 'WORLD'
    _set_constraint_axes(constraint, mapping)


def _add_copy_transforms_constraint(target_object, rigify_bone):
    constraint = rigify_bone.constraints.new('COPY_TRANSFORMS')
    constraint.name = CONSTRAINT_TAG
    constraint.target = target_object
    constraint.target_space = 'WORLD'
    constraint.owner_space = 'WORLD'


def _ensure_all_bones_visible(rigify_rig):
    collection_state = {}
    for bone_collection in getattr(rigify_rig.data, 'collections_all', []):
        collection_state[bone_collection.name] = (bone_collection.is_visible, bone_collection.is_solo)
        bone_collection.is_visible = True
        bone_collection.is_solo = False

    bone_state = {}
    for pose_bone in rigify_rig.pose.bones:
        data_bone = pose_bone.bone
        bone_state[pose_bone.name] = (data_bone.hide, pose_bone.select)
        data_bone.hide = False
        pose_bone.select = False

    return collection_state, bone_state


def _restore_visibility(rigify_rig, collection_state, bone_state):
    for pose_bone in rigify_rig.pose.bones:
        state = bone_state.get(pose_bone.name)
        if state:
            pose_bone.bone.hide, pose_bone.select = state

    for bone_collection in getattr(rigify_rig.data, 'collections_all', []):
        state = collection_state.get(bone_collection.name)
        if state:
            bone_collection.is_visible, bone_collection.is_solo = state


def _get_or_create_target_action(scene, source_action, rigify_rig):
    if scene.udr_rigify_action:
        rigify_rig.animation_data.action = scene.udr_rigify_action
        return scene.udr_rigify_action

    target_action = bpy.data.actions.get(source_action.name)
    if not target_action:
        target_action = bpy.data.actions.new(source_action.name)

    target_action.use_fake_user = True
    rigify_rig.animation_data.action = target_action
    return target_action


def _bake_boundary_frames(start_frame, end_frame):
    """Re-bake the clip's first/last frames so looping stays seamless.

    ``bpy.ops.nla.bake`` steps forward from ``frame_start`` and is not
    guaranteed to land exactly on ``frame_end`` when ``step`` does not evenly
    divide the frame range, which would otherwise drop the final key needed
    for a clean loop back to the first frame.
    """
    for frame in {start_frame, end_frame}:
        bpy.ops.nla.bake(
            frame_start=frame,
            frame_end=frame,
            step=1,
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            use_current_action=True,
            bake_types={'POSE'}
        )


def _get_bake_control_names(scene, rigify_rig):
    control_names = []
    for mapping in get_profile_mappings(scene, rigify_rig):
        control_name = mapping['control']
        if control_name not in control_names:
            control_names.append(control_name)
    return control_names


def _select_bake_controls(rigify_rig, scene):
    bpy.ops.pose.select_all(action='DESELECT')

    for control_name in _get_bake_control_names(scene, rigify_rig):
        # Blender 5.0+ removed Bone.select; selection now lives on PoseBone.
        pose_bone = rigify_rig.pose.bones.get(control_name)
        if pose_bone:
            pose_bone.select = True


def _action_channelbags(action):
    """Iterate all F-Curve channelbags of a layered action."""
    for layer in action.layers:
        for strip in layer.strips:
            channelbags = getattr(strip, 'channelbags', None)
            if channelbags is not None:
                yield from channelbags


def _iter_action_fcurves(action):
    """Iterate an action's F-Curves across legacy and layered-action forms.

    Blender 5.1 removed the ``Action.fcurves`` compatibility shim; F-Curves
    now live under channelbags nested in the action's layers/strips.
    """
    legacy_fcurves = getattr(action, 'fcurves', None)
    if legacy_fcurves is not None:
        yield from legacy_fcurves
        return
    for channelbag in _action_channelbags(action):
        yield from channelbag.fcurves


def _remove_action_fcurve(action, fcurve):
    """Remove a single F-Curve, locating its channelbag on layered actions."""
    legacy_fcurves = getattr(action, 'fcurves', None)
    if legacy_fcurves is not None:
        legacy_fcurves.remove(fcurve)
        return
    for channelbag in _action_channelbags(action):
        for candidate in channelbag.fcurves:
            if candidate == fcurve:
                channelbag.fcurves.remove(fcurve)
                return


def _bone_transform_curves(action, pose_bone):
    bone_path_prefix = f'{pose_bone.path_from_id()}.'
    transform_properties = {
        'location',
        'rotation_axis_angle',
        'rotation_euler',
        'rotation_quaternion',
        'scale',
    }
    curves = {property_name: [] for property_name in transform_properties}
    for fcurve in _iter_action_fcurves(action):
        if not fcurve.data_path.startswith(bone_path_prefix):
            continue
        property_name = fcurve.data_path[len(bone_path_prefix):]
        if property_name in curves:
            curves[property_name].append(fcurve)
    return curves


def _curve_frames(fcurves, start_frame, end_frame):
    frames = {float(start_frame), float(end_frame)}
    for fcurve in fcurves:
        for keyframe in fcurve.keyframe_points:
            frame = keyframe.co[0]
            if start_frame <= frame <= end_frame:
                frames.add(frame)
    return sorted(frames)


def _curve_components(fcurves, frame, defaults):
    values = list(defaults)
    for fcurve in fcurves:
        if fcurve.array_index < len(values):
            values[fcurve.array_index] = fcurve.evaluate(frame)
    return values


def _curves_are_clip_local(fcurves, start_frame, end_frame):
    return all(
        start_frame <= keyframe.co[0] <= end_frame
        for fcurve in fcurves
        for keyframe in fcurve.keyframe_points
    )


def _location_is_negligible(fcurves, start_frame, end_frame, threshold):
    return all(
        math.sqrt(sum(component * component for component in _curve_components(
            fcurves, frame, (0.0, 0.0, 0.0)))) <= threshold
        for frame in _curve_frames(fcurves, start_frame, end_frame)
    )


def _scale_is_negligible(fcurves, start_frame, end_frame, threshold):
    return all(
        max(abs(component - 1.0) for component in _curve_components(
            fcurves, frame, (1.0, 1.0, 1.0))) <= threshold
        for frame in _curve_frames(fcurves, start_frame, end_frame)
    )


def _quaternion_angle(components):
    length = math.sqrt(sum(component * component for component in components))
    if length <= 1e-12:
        return math.pi
    normalized_w = max(-1.0, min(1.0, components[0] / length))
    return 2.0 * math.acos(abs(normalized_w))


def _normalized_quaternion(components):
    length = math.sqrt(sum(component * component for component in components))
    if length <= 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    return tuple(component / length for component in components)


def _axis_angle_quaternion(components):
    angle, axis_x, axis_y, axis_z = components
    axis_length = math.sqrt(axis_x ** 2 + axis_y ** 2 + axis_z ** 2)
    if axis_length <= 1e-12:
        return (1.0, 0.0, 0.0, 0.0)
    half_angle = angle * 0.5
    scale = math.sin(half_angle) / axis_length
    return _normalized_quaternion((
        math.cos(half_angle),
        axis_x * scale,
        axis_y * scale,
        axis_z * scale,
    ))


def _slerp_quaternion(first, second, factor):
    first = _normalized_quaternion(first)
    second = _normalized_quaternion(second)
    dot = sum(a * b for a, b in zip(first, second))
    if dot < 0.0:
        second = tuple(-component for component in second)
        dot = -dot
    dot = max(-1.0, min(1.0, dot))

    if dot > 0.9995:
        return _normalized_quaternion(tuple(
            a + factor * (b - a)
            for a, b in zip(first, second)
        ))

    angle = math.acos(dot)
    sin_angle = math.sin(angle)
    first_weight = math.sin((1.0 - factor) * angle) / sin_angle
    second_weight = math.sin(factor * angle) / sin_angle
    return tuple(
        first_weight * a + second_weight * b
        for a, b in zip(first, second)
    )


def _quaternion_distance(first, second):
    first = _normalized_quaternion(first)
    second = _normalized_quaternion(second)
    dot = abs(sum(a * b for a, b in zip(first, second)))
    return 2.0 * math.acos(max(-1.0, min(1.0, dot)))


def _rotation_is_negligible(
    rotation_property,
    fcurves,
    start_frame,
    end_frame,
    threshold,
):
    if rotation_property == 'rotation_quaternion':
        defaults = (1.0, 0.0, 0.0, 0.0)
        angle_at_frame = lambda frame: _quaternion_angle(
            _curve_components(fcurves, frame, defaults))
    elif rotation_property == 'rotation_axis_angle':
        defaults = (0.0, 0.0, 1.0, 0.0)
        angle_at_frame = lambda frame: abs(
            _curve_components(fcurves, frame, defaults)[0])
    else:
        defaults = (0.0, 0.0, 0.0)
        # For the deliberately small cleanup threshold, the Euler vector norm
        # is a conservative approximation of the orientation's angular delta.
        angle_at_frame = lambda frame: math.sqrt(sum(
            component * component
            for component in _curve_components(fcurves, frame, defaults)
        ))

    return all(
        angle_at_frame(frame) <= threshold
        for frame in _curve_frames(fcurves, start_frame, end_frame)
    )


def _interpolation_error(
    property_name,
    actual,
    first,
    second,
    factor,
):
    if property_name == 'rotation_quaternion':
        expected = _slerp_quaternion(first, second, factor)
        return _quaternion_distance(actual, expected)
    if property_name == 'rotation_axis_angle':
        expected = _slerp_quaternion(
            _axis_angle_quaternion(first),
            _axis_angle_quaternion(second),
            factor,
        )
        return _quaternion_distance(
            _axis_angle_quaternion(actual), expected)

    expected = tuple(
        a + factor * (b - a)
        for a, b in zip(first, second)
    )
    if property_name == 'rotation_euler':
        angular_deltas = [
            (value - target + math.pi) % (2.0 * math.pi) - math.pi
            for value, target in zip(actual, expected)
        ]
        return math.sqrt(sum(delta * delta for delta in angular_deltas))
    if property_name == 'scale':
        return max(abs(value - target) for value, target in zip(actual, expected))
    return math.sqrt(sum(
        (value - target) ** 2
        for value, target in zip(actual, expected)
    ))


def _simplified_sample_indices(property_name, frames, values, threshold):
    if len(frames) <= 2:
        return set(range(len(frames)))

    kept_indices = {0, len(frames) - 1}
    spans = [(0, len(frames) - 1)]
    while spans:
        first_index, last_index = spans.pop()
        frame_span = frames[last_index] - frames[first_index]
        if frame_span <= 0.0:
            continue

        largest_error = -1.0
        largest_error_index = None
        for sample_index in range(first_index + 1, last_index):
            factor = (
                (frames[sample_index] - frames[first_index]) / frame_span
            )
            error = _interpolation_error(
                property_name,
                values[sample_index],
                values[first_index],
                values[last_index],
                factor,
            )
            if error > largest_error:
                largest_error = error
                largest_error_index = sample_index

        if largest_error_index is not None and largest_error > threshold:
            kept_indices.add(largest_error_index)
            spans.append((first_index, largest_error_index))
            spans.append((largest_error_index, last_index))

    return kept_indices


def _simplify_transform_curves(
    property_name,
    fcurves,
    start_frame,
    end_frame,
    threshold,
):
    frames = sorted({
        keyframe.co[0]
        for fcurve in fcurves
        for keyframe in fcurve.keyframe_points
        if start_frame <= keyframe.co[0] <= end_frame
    })
    if len(frames) <= 2:
        return 0
    defaults = {
        'location': (0.0, 0.0, 0.0),
        'rotation_axis_angle': (0.0, 0.0, 1.0, 0.0),
        'rotation_euler': (0.0, 0.0, 0.0),
        'rotation_quaternion': (1.0, 0.0, 0.0, 0.0),
        'scale': (1.0, 1.0, 1.0),
    }[property_name]
    values = [
        tuple(_curve_components(fcurves, frame, defaults))
        for frame in frames
    ]
    kept_indices = _simplified_sample_indices(
        property_name, frames, values, threshold)
    kept_frames = {frames[index] for index in kept_indices}

    removed_count = 0
    for fcurve in fcurves:
        for keyframe in list(fcurve.keyframe_points):
            if (
                start_frame <= keyframe.co[0] <= end_frame
                and keyframe.co[0] not in kept_frames
            ):
                fcurve.keyframe_points.remove(keyframe)
                removed_count += 1
        for keyframe in fcurve.keyframe_points:
            if start_frame <= keyframe.co[0] <= end_frame:
                keyframe.interpolation = 'LINEAR'
        fcurve.update()
    return removed_count


def _remove_curves(action, fcurves):
    for fcurve in list(fcurves):
        _remove_action_fcurve(action, fcurve)
    return len(fcurves)


def _reset_transform_property(pose_bone, property_name):
    if property_name == 'location':
        pose_bone.location = (0.0, 0.0, 0.0)
    elif property_name == 'scale':
        pose_bone.scale = (1.0, 1.0, 1.0)
    elif property_name == 'rotation_quaternion':
        pose_bone.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    elif property_name == 'rotation_axis_angle':
        pose_bone.rotation_axis_angle = (0.0, 0.0, 1.0, 0.0)
    elif property_name == 'rotation_euler':
        pose_bone.rotation_euler = (0.0, 0.0, 0.0)


def _remove_clip_local_transform_curves(
    action,
    pose_bone,
    start_frame,
    end_frame,
):
    removed_count = 0
    for property_name, fcurves in _bone_transform_curves(
        action, pose_bone).items():
        if not fcurves or not _curves_are_clip_local(
            fcurves, start_frame, end_frame):
            continue
        removed_count += _remove_curves(action, fcurves)
        _reset_transform_property(pose_bone, property_name)
    return removed_count


def _remove_ik_conflict_curves(
    action,
    rigify_rig,
    start_frame,
    end_frame,
):
    """Remove stale limb FK/tweak data when explicitly authoring in IK."""
    removed_count = 0
    for pose_bone in rigify_rig.pose.bones:
        if _is_limb_fk_or_tweak_control(pose_bone.name):
            removed_count += _remove_clip_local_transform_curves(
                action,
                pose_bone,
                start_frame,
                end_frame,
            )
    return removed_count


def _remove_torso_conflict_curves(
    action,
    rigify_rig,
    start_frame,
    end_frame,
):
    """Remove direct spine FK/tweak data in high-level torso mode."""
    removed_count = 0
    for pose_bone in rigify_rig.pose.bones:
        if _is_spine_fk_or_tweak_control(pose_bone.name):
            removed_count += _remove_clip_local_transform_curves(
                action,
                pose_bone,
                start_frame,
                end_frame,
            )
    return removed_count


def _remove_finger_control_curves(
    action,
    rigify_rig,
    start_frame,
    end_frame,
):
    """Remove stale individual finger animation when finger baking is off."""
    removed_count = 0
    for pose_bone in rigify_rig.pose.bones:
        if _is_finger_control(pose_bone.name):
            removed_count += _remove_clip_local_transform_curves(
                action,
                pose_bone,
                start_frame,
                end_frame,
            )
    return removed_count


def _key_ik_mode(action, rigify_rig, start_frame, end_frame):
    """Keep the baked action in IK after the live preview state is restored."""
    for pose_bone in rigify_rig.pose.bones:
        if not isinstance(pose_bone.get(IK_FK_PROPERTY), float):
            continue

        data_path = f'["{IK_FK_PROPERTY}"]'
        full_data_path = pose_bone.path_from_id(data_path)
        for fcurve in _iter_action_fcurves(action):
            if fcurve.data_path != full_data_path:
                continue
            for keyframe in list(fcurve.keyframe_points):
                if start_frame <= keyframe.co[0] <= end_frame:
                    fcurve.keyframe_points.remove(keyframe)

        pose_bone[IK_FK_PROPERTY] = 0.0
        pose_bone.keyframe_insert(
            data_path=data_path,
            frame=start_frame,
            group=pose_bone.name,
        )
        if end_frame != start_frame:
            pose_bone.keyframe_insert(
                data_path=data_path,
                frame=end_frame,
                group=pose_bone.name,
            )

        for fcurve in _iter_action_fcurves(action):
            if fcurve.data_path != full_data_path:
                continue
            for keyframe in fcurve.keyframe_points:
                if keyframe.co[0] in {start_frame, end_frame}:
                    keyframe.interpolation = 'CONSTANT'


def _clean_negligible_bake(
    scene,
    action,
    rigify_rig,
    control_names,
    start_frame,
    end_frame,
):
    """Remove neutral channels and reduce dense keys within error limits."""
    removed_count = 0
    for control_name in control_names:
        pose_bone = rigify_rig.pose.bones.get(control_name)
        if not pose_bone:
            continue

        curves_by_property = _bone_transform_curves(action, pose_bone)
        for property_name, fcurves in curves_by_property.items():
            if not fcurves or not _curves_are_clip_local(
                fcurves, start_frame, end_frame):
                continue

            if property_name == 'location':
                is_negligible = _location_is_negligible(
                    fcurves,
                    start_frame,
                    end_frame,
                    scene.udr_location_threshold,
                )
            elif property_name == 'scale':
                is_negligible = _scale_is_negligible(
                    fcurves,
                    start_frame,
                    end_frame,
                    scene.udr_scale_threshold,
                )
            else:
                is_negligible = _rotation_is_negligible(
                    property_name,
                    fcurves,
                    start_frame,
                    end_frame,
                    scene.udr_rotation_threshold,
                )

            if is_negligible:
                removed_count += _remove_curves(action, fcurves)
                _reset_transform_property(pose_bone, property_name)
            else:
                threshold = {
                    'location': scene.udr_location_threshold,
                    'scale': scene.udr_scale_threshold,
                }.get(property_name, scene.udr_rotation_threshold)
                removed_count += _simplify_transform_curves(
                    property_name,
                    fcurves,
                    start_frame,
                    end_frame,
                    threshold,
                )

    return removed_count


def add_constraints(scene):
    source_rig = scene.udr_source_rig
    rigify_rig = scene.udr_rigify_rig
    if not source_rig or not rigify_rig:
        raise ValueError('Both source and Rigify armatures must be selected.')

    _validate_authoring_controls(scene, source_rig, rigify_rig)

    previous_object, previous_mode = ensure_pose_mode(rigify_rig)

    try:
        # Rebuilding an enabled preview (for example after changing drive mode)
        # must not overwrite the user's original IK/FK state with the temporary
        # state imposed by the previous preview.
        if not scene.udr_saved_ikfk_state:
            scene.udr_saved_ikfk_state = json.dumps(
                _save_ikfk_state(rigify_rig))
        _set_drive_mode(rigify_rig, scene.udr_ik_driven)
        _suspend_target_action_for_preview(scene, rigify_rig)
        _clear_existing_constraints(rigify_rig)
        _clear_offset_helpers()

        for mapping in get_profile_mappings(scene, rigify_rig):
            rigify_bone = rigify_rig.pose.bones.get(mapping['control'])
            if not rigify_bone:
                continue

            source_bone_name = _resolve_source_bone_name(
                source_rig, mapping['source'])
            if not source_bone_name:
                continue

            offset_target = _create_offset_helpers(
                source_rig, rigify_rig, source_bone_name, rigify_bone.name)
            if not offset_target:
                continue

            if mapping['type'] == COPY_ROTATION:
                _add_copy_rotation_constraint(
                    offset_target, rigify_bone, mapping)
            elif mapping['type'] == COPY_LOCATION:
                _add_copy_location_constraint(
                    offset_target, rigify_bone, mapping)
            else:
                # ROOT links also use a rest-relative COPY_TRANSFORMS chain.
                # This mirrors UE2Rigify and keeps a static source object from
                # resetting or animating the Rigify master controls.
                _add_copy_transforms_constraint(offset_target, rigify_bone)
    finally:
        restore_mode(previous_object, previous_mode)


def remove_constraints(scene):
    rigify_rig = scene.udr_rigify_rig
    if not rigify_rig:
        raise ValueError('A Rigify armature must be selected.')

    previous_object, previous_mode = ensure_pose_mode(rigify_rig)
    try:
        _clear_existing_constraints(rigify_rig)
        _clear_offset_helpers()
        _restore_ikfk_state(rigify_rig, scene.udr_saved_ikfk_state)
        _restore_target_action_after_preview(scene, rigify_rig)
        scene.udr_saved_ikfk_state = ''
    finally:
        restore_mode(previous_object, previous_mode)


def bake_to_rigify(scene):
    source_rig = scene.udr_source_rig
    rigify_rig = scene.udr_rigify_rig
    source_action = scene.udr_source_action

    if not source_rig or not rigify_rig:
        raise ValueError('Both source and Rigify armatures must be selected.')
    if not source_action:
        raise ValueError('A source action must be selected.')

    _validate_authoring_controls(scene, source_rig, rigify_rig)

    if source_rig.animation_data is None:
        source_rig.animation_data_create()
    if rigify_rig.animation_data is None:
        rigify_rig.animation_data_create()

    source_animation_data_block = source_rig.animation_data
    rigify_animation_data_block = rigify_rig.animation_data
    if source_animation_data_block is None or rigify_animation_data_block is None:
        raise ValueError('Source and Rigify rigs must both support animation data.')

    previous_source_action = source_animation_data_block.action
    source_animation_data_block.action = source_action

    previous_target_action = rigify_animation_data_block.action

    previous_object, previous_mode = ensure_pose_mode(rigify_rig)
    collection_state, bone_state = _ensure_all_bones_visible(rigify_rig)
    removed_curve_count = 0

    try:
        target_action = _get_or_create_target_action(scene, source_action, rigify_rig)
        overwrite = bool(scene.udr_rigify_action)
        bake_control_names = _get_bake_control_names(scene, rigify_rig)
        _select_bake_controls(rigify_rig, scene)

        start_frame, end_frame = map(int, source_action.frame_range)

        bpy.ops.nla.bake(
            frame_start=start_frame,
            frame_end=end_frame,
            step=scene.udr_keyframe_step,
            only_selected=True,
            visual_keying=True,
            clear_constraints=False,
            use_current_action=overwrite,
            bake_types={'POSE'}
        )

        rigify_animation_data_block.action = target_action

        if scene.udr_keyframe_step > 1:
            _bake_boundary_frames(start_frame, end_frame)

        if scene.udr_torso_controls:
            removed_curve_count += _remove_torso_conflict_curves(
                target_action,
                rigify_rig,
                start_frame,
                end_frame,
            )

        if not scene.udr_bake_fingers:
            removed_curve_count += _remove_finger_control_curves(
                target_action,
                rigify_rig,
                start_frame,
                end_frame,
            )

        if scene.udr_ik_driven:
            removed_curve_count += _remove_ik_conflict_curves(
                target_action,
                rigify_rig,
                start_frame,
                end_frame,
            )
            _key_ik_mode(
                target_action,
                rigify_rig,
                start_frame,
                end_frame,
            )

        if scene.udr_clean_bake:
            removed_curve_count += _clean_negligible_bake(
                scene,
                target_action,
                rigify_rig,
                bake_control_names,
                start_frame,
                end_frame,
            )

        # The baked result should remain active when Retarget is turned off,
        # rather than restoring the action that was muted for the preview.
        scene.udr_preview_action = target_action

        if not scene.udr_rigify_action:
            scene.udr_rigify_action = rigify_animation_data_block.action
    finally:
        _restore_visibility(rigify_rig, collection_state, bone_state)
        restore_mode(previous_object, previous_mode)
        source_animation_data_block.action = previous_source_action
        rigify_animation_data_block.action = previous_target_action or rigify_animation_data_block.action

    return removed_curve_count


class UDR_OT_retarget(bpy.types.Operator):
    bl_idname = 'udr.retarget'
    bl_label = 'Retarget'

    def execute(self, context):
        try:
            add_constraints(context.scene)
        except ValueError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class UDR_OT_untarget(bpy.types.Operator):
    bl_idname = 'udr.untarget'
    bl_label = 'Untarget'

    def execute(self, context):
        try:
            remove_constraints(context.scene)
        except ValueError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        return {'FINISHED'}


class UDR_OT_bake_fk(bpy.types.Operator):
    bl_idname = 'udr.bake_fk'
    bl_label = 'Bake FK'

    def invoke(self, context, event):
        if context.scene.udr_rigify_action:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        try:
            removed_curve_count = bake_to_rigify(context.scene)
        except ValueError as error:
            self.report({'ERROR'}, str(error))
            return {'CANCELLED'}
        if removed_curve_count:
            self.report(
                {'INFO'},
                (
                    f'Bake complete; removed {removed_curve_count} '
                    'redundant curves/keys.'
                ),
            )
        return {'FINISHED'}


class UDR_OT_toggle_retarget(bpy.types.Operator):
    bl_idname = 'udr.toggle_retarget'
    bl_label = 'Toggle Retarget'

    def execute(self, context):
        scene = context.scene
        if scene.udr_enabled:
            bpy.ops.udr.retarget('INVOKE_DEFAULT')
        else:
            bpy.ops.udr.untarget('INVOKE_DEFAULT')
        return {'FINISHED'}


class UDR_PT_panel(bpy.types.Panel):
    bl_label = 'Retarget Source -> Rigify'
    bl_idname = 'VIEW3D_PT_udr_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Item'
    bl_order = 100

    def draw(self, context):
        scene = context.scene
        layout = self.layout

        required_properties = [
            'udr_source_rig',
            'udr_source_action',
            'udr_rigify_rig',
            'udr_rigify_action',
            'udr_profile',
            'udr_torso_controls',
            'udr_bake_fingers',
            'udr_ik_driven',
            'udr_keyframe_step',
            'udr_clean_bake',
            'udr_location_threshold',
            'udr_rotation_threshold',
            'udr_scale_threshold',
            'udr_enabled',
        ]
        if not all(hasattr(scene, property_name) for property_name in required_properties):
            layout.label(text='Addon registration is incomplete.', icon='ERROR')
            return

        source_box = layout.box()
        source_box.label(text='Source')
        source_box.prop(scene, 'udr_source_rig', text='Rig')
        source_box.prop(scene, 'udr_source_action', text='Action')

        target_box = layout.box()
        target_box.label(text='Rigify Target')
        target_box.prop(scene, 'udr_rigify_rig', text='Rig')
        target_box.prop(scene, 'udr_rigify_action', text='Action')

        layout.prop(scene, 'udr_profile', text='Profile')

        options_box = layout.box()
        options_box.label(text='Authoring Options')
        options_box.prop(scene, 'udr_torso_controls')
        options_box.prop(scene, 'udr_bake_fingers')
        options_box.prop(scene, 'udr_ik_driven')
        options_box.prop(scene, 'udr_keyframe_step')
        options_box.prop(scene, 'udr_clean_bake')
        if scene.udr_clean_bake:
            threshold_column = options_box.column(align=True)
            threshold_column.prop(scene, 'udr_location_threshold')
            threshold_column.prop(scene, 'udr_rotation_threshold')
            threshold_column.prop(scene, 'udr_scale_threshold')

        row = layout.row(align=True)
        row.prop(scene, 'udr_enabled', toggle=True, icon='CONSTRAINT')
        bake_col = row.column(align=True)
        bake_col.operator(
            'udr.bake_fk',
            text='Bake IK' if scene.udr_ik_driven else 'Bake FK',
            icon='CAMERA_DATA',
        )
        bake_col.enabled = scene.udr_enabled


classes = (
    UDR_OT_retarget,
    UDR_OT_untarget,
    UDR_OT_bake_fk,
    UDR_OT_toggle_retarget,
    UDR_PT_panel,
)


@bpy.app.handlers.persistent
def _rebuild_enabled_retargets(_unused=None):
    """Replace constraints saved by an older add-on version after loading."""
    scenes = getattr(bpy.data, 'scenes', None)
    if scenes is None:
        return

    for scene in scenes:
        if not getattr(scene, 'udr_enabled', False):
            continue
        try:
            add_constraints(scene)
        except Exception as error:
            print(
                f'Direct Retarget: could not rebuild scene '
                f'{scene.name!r}: {error}'
            )


def _deferred_rebuild_enabled_retargets():
    """Run the initial rebuild after Blender releases restricted data access."""
    if not hasattr(bpy.data, 'scenes'):
        return 0.1
    _rebuild_enabled_retargets()
    return None


def register():
    for addon_class in classes:
        bpy.utils.register_class(addon_class)

    bpy.types.Scene.udr_source_rig = bpy.props.PointerProperty(
        name='Source Rig',
        type=bpy.types.Object,
        poll=armature_poll,
        update=source_rig_update
    )
    bpy.types.Scene.udr_source_action = bpy.props.PointerProperty(
        name='Source Action',
        type=bpy.types.Action,
        poll=action_poll
    )
    bpy.types.Scene.udr_rigify_rig = bpy.props.PointerProperty(
        name='Rigify Rig',
        type=bpy.types.Object,
        poll=armature_poll,
        update=rigify_rig_update
    )
    bpy.types.Scene.udr_rigify_action = bpy.props.PointerProperty(
        name='Rigify Action',
        type=bpy.types.Action,
        poll=action_poll
    )
    bpy.types.Scene.udr_profile = bpy.props.EnumProperty(
        name='Profile',
        items=PROFILE_ITEMS,
        default='UE5_MANNY',
        update=retarget_settings_update,
    )
    bpy.types.Scene.udr_torso_controls = bpy.props.BoolProperty(
        name='High-Level Torso Controls',
        description=(
            'Author the spine on torso, hips, and chest instead of individual '
            'spine FK and tweak controls'
        ),
        default=False,
        update=retarget_settings_update,
    )
    bpy.types.Scene.udr_bake_fingers = bpy.props.BoolProperty(
        name='Include Finger FKs',
        description=(
            'Add animation to individual thumb and finger FK controls'
        ),
        default=True,
        update=retarget_settings_update,
    )
    bpy.types.Scene.udr_ik_driven = bpy.props.BoolProperty(
        name='IK Driven Limbs',
        description=(
            'Author arms and legs with Rigify IK end effectors and pole '
            'controls instead of competing limb FK and tweak controls'
        ),
        default=False,
        update=retarget_settings_update,
    )
    bpy.types.Scene.udr_keyframe_step = bpy.props.IntProperty(
        name='Keyframe Step',
        description=(
            'Number of frames to skip forward while baking; higher values '
            'produce fewer keyframes. The first and last frames are always '
            'kept so looping stays seamless'
        ),
        default=1,
        min=1,
        soft_max=10,
    )
    bpy.types.Scene.udr_clean_bake = bpy.props.BoolProperty(
        name='Skip Negligible Motion',
        description=(
            'Remove neutral transform channels and reduce dense baked keys '
            'while staying within the configured error thresholds'
        ),
        default=False,
    )
    bpy.types.Scene.udr_location_threshold = bpy.props.FloatProperty(
        name='Max Location Error',
        description='Maximum location error allowed while cleaning the bake',
        default=0.001,
        min=0.0,
        soft_max=0.1,
        precision=4,
        subtype='DISTANCE',
    )
    bpy.types.Scene.udr_rotation_threshold = bpy.props.FloatProperty(
        name='Max Rotation Error',
        description='Maximum angular error allowed while cleaning the bake',
        default=math.radians(0.1),
        min=0.0,
        soft_max=math.radians(10.0),
        subtype='ANGLE',
    )
    bpy.types.Scene.udr_scale_threshold = bpy.props.FloatProperty(
        name='Max Scale Error',
        description='Maximum scale error allowed while cleaning the bake',
        default=0.001,
        min=0.0,
        soft_max=0.1,
        precision=4,
    )
    bpy.types.Scene.udr_enabled = bpy.props.BoolProperty(
        name='Retarget',
        description='Enable or disable direct retarget constraints.',
        default=False,
        update=lambda self, context: bpy.ops.udr.toggle_retarget() and None
    )
    bpy.types.Scene.udr_preview_action = bpy.props.PointerProperty(
        type=bpy.types.Action
    )
    bpy.types.Scene.udr_saved_ikfk_state = bpy.props.StringProperty(default='')

    if _rebuild_enabled_retargets not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_rebuild_enabled_retargets)
    if hasattr(bpy.data, 'scenes'):
        _rebuild_enabled_retargets()
    elif not bpy.app.timers.is_registered(_deferred_rebuild_enabled_retargets):
        bpy.app.timers.register(
            _deferred_rebuild_enabled_retargets,
            first_interval=0.0,
        )


def unregister():
    if _rebuild_enabled_retargets in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_rebuild_enabled_retargets)
    if bpy.app.timers.is_registered(_deferred_rebuild_enabled_retargets):
        bpy.app.timers.unregister(_deferred_rebuild_enabled_retargets)

    del bpy.types.Scene.udr_saved_ikfk_state
    del bpy.types.Scene.udr_preview_action
    del bpy.types.Scene.udr_enabled
    del bpy.types.Scene.udr_scale_threshold
    del bpy.types.Scene.udr_rotation_threshold
    del bpy.types.Scene.udr_location_threshold
    del bpy.types.Scene.udr_clean_bake
    del bpy.types.Scene.udr_keyframe_step
    del bpy.types.Scene.udr_ik_driven
    del bpy.types.Scene.udr_bake_fingers
    del bpy.types.Scene.udr_torso_controls
    del bpy.types.Scene.udr_profile
    del bpy.types.Scene.udr_rigify_action
    del bpy.types.Scene.udr_rigify_rig
    del bpy.types.Scene.udr_source_action
    del bpy.types.Scene.udr_source_rig

    for addon_class in reversed(classes):
        bpy.utils.unregister_class(addon_class)
