"""Fresh validation and isolated export; existing files are never overwritten."""
import bpy
import json
import html
from pathlib import Path
from .core import selected, unique_path, safe_name, scope, log
from .analysis import refresh

def export_asset(context):
    s = context.scene.gr
    objects = scope(context)
    if not objects:
        raise ValueError('Nothing to export. Select an asset first.')
    result = refresh(context, objects)
    errors = [i for i in result['issues'] if i['severity']=='ERROR']
    if errors and not s.override_errors:
        raise ValueError(f'{len(errors)} critical validation issues remain. Review the scan or explicitly enable critical-issue override.')
    names = {o.name for o in objects}
    helpers = [o for o in context.scene.objects if (s.export_lods and o.get('gr_lod_source') in names) or (s.export_colliders and o.get('gr_collision_source') in names)]
    # Export LOD0 instead of a duplicate base mesh when a generated chain is included.
    replaced = {o.get('gr_lod_source') for o in helpers if o.get('gr_lod_level') == 0}
    objects = [o for o in objects if o.name not in replaced] + helpers
    hidden = [(o, o.hide_get(), o.hide_render) for o in objects]
    mods = []
    path = unique_path(Path(bpy.path.abspath(s.output_dir)) / (safe_name(s.export_name) + {'FBX':'.fbx','GLB':'.glb','OBJ':'.obj'}[s.export_format]))
    try:
        for obj, _, _ in hidden:
            obj.hide_set(False)
            obj.hide_render = False
            if s.triangulate and obj.type == 'MESH':
                mods.append((obj, obj.modifiers.new('GameReady Export Triangulate', 'TRIANGULATE')))
        with selected(objects):
            if s.export_format == 'FBX':
                bpy.ops.export_scene.fbx(filepath=str(path), use_selection=True, global_scale=s.export_scale, axis_forward=s.axis_forward, axis_up=s.axis_up, use_mesh_modifiers=True, add_leaf_bones=False, bake_anim=s.export_animation, object_types={'MESH','EMPTY','ARMATURE'} if s.export_armatures else {'MESH','EMPTY'}, path_mode='COPY', embed_textures=s.embed_textures)
            elif s.export_format == 'GLB':
                if s.export_scale != 1:
                    raise ValueError('GLB uses scene units. Set export scale to 1; adjust scene units explicitly if needed.')
                bpy.ops.export_scene.gltf(filepath=str(path), export_format='GLB', use_selection=True, export_apply=True, export_animations=s.export_animation, export_skins=s.export_armatures, export_materials='EXPORT' if s.export_materials else 'NONE', export_yup=True)
            else:
                bpy.ops.wm.obj_export(filepath=str(path), export_selected_objects=True, global_scale=s.export_scale, forward_axis=s.axis_forward.replace('-','NEGATIVE_'), up_axis=s.axis_up.replace('-','NEGATIVE_'), export_materials=s.export_materials, apply_modifiers=True)
    finally:
        for obj, modifier in mods:
            obj.modifiers.remove(modifier)
        for obj, hide, render in hidden:
            obj.hide_set(hide)
            obj.hide_render = render
    if not path.exists() or not path.stat().st_size:
        raise RuntimeError('Exporter did not produce a non-empty file.')
    log(context.scene, f'Exported {path.name}; engine import still requires testing.')
    return path

def write_report(context):
    from .core import report
    data = report(context.scene)
    refresh(context)
    data = report(context.scene)
    data = dict(data, history=[i.message for i in context.scene.gr_history])
    root = Path(bpy.path.abspath(context.scene.gr.output_dir))
    json_path = unique_path(root / 'GameReady_Report.json')
    json_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding='utf-8')
    lines = ['GAME READY REPORT', f'Blender-side readiness: {data["score"]}/100', f'Profile: {data["engine"]}', '', 'Original: ' + json.dumps(data.get('before', {})), 'Current: ' + json.dumps(data['statistics']), '']
    lines += [f'{i["severity"]} | {i["object"]} | {i["message"]} | {i["recommendation"]}' for i in data['issues']]
    lines += ['', 'LIMITATIONS'] + data['limitations'] + ['', 'HISTORY'] + data['history']
    text = '\n'.join(lines)
    unique_path(root / 'GameReady_Report.txt').write_text(text, encoding='utf-8')
    unique_path(root / 'GameReady_Report.html').write_text('<!doctype html><meta charset="utf-8"><title>GameReady report</title><style>body{background:#101822;color:#e4ecf2;font:16px system-ui;padding:48px}pre{white-space:pre-wrap;line-height:1.6}</style><h1>GameReady One-Click</h1><pre>' + html.escape(text) + '</pre>', encoding='utf-8')
    return json_path
