"""Scope, evaluated statistics, isolation and reproducible session records."""
import bpy
import json
import re
from datetime import datetime, timezone
from contextlib import contextmanager

CATEGORIES = ('Geometry', 'UVs', 'Materials', 'Textures', 'Transforms', 'Naming', 'Performance', 'Export')

def scope(context):
    mode = context.scene.gr.scope
    objects = context.selected_objects if mode == 'SELECTED' else context.collection.all_objects if mode == 'COLLECTION' else context.scene.objects
    return sorted([o for o in objects if not o.get('gr_backup') and not o.get('gr_helper')], key=lambda o: o.name)

def images(objects):
    found = {}
    def walk(tree, seen):
        if not tree or tree.as_pointer() in seen:
            return
        seen.add(tree.as_pointer())
        for node in tree.nodes:
            if node.type == 'TEX_IMAGE' and node.image:
                found[node.image.name] = node.image
            if node.type == 'GROUP':
                walk(node.node_tree, seen)
    for obj in objects:
        for slot in obj.material_slots:
            if slot.material:
                walk(slot.material.node_tree, set())
    return list(found.values())

def memory(image):
    # Uncompressed GPU estimate, full mip chain; not file size or measured VRAM.
    return int(image.size[0] * image.size[1] * max(image.channels, 1) * (4 if image.is_float else 1) * 4 / 3)

def stats(objects):
    result = dict(objects=len(objects), vertices=0, triangles=0, materials=0, textures=0, texture_bytes=0, material_slots=0)
    mats = set()
    depsgraph = bpy.context.evaluated_depsgraph_get()
    for obj in objects:
        if obj.type == 'MESH':
            evaluated = obj.evaluated_get(depsgraph)
            mesh = evaluated.to_mesh()
            try:
                mesh.calc_loop_triangles()
                result['vertices'] += len(mesh.vertices)
                result['triangles'] += len(mesh.loop_triangles)
                result['material_slots'] += len({p.material_index for p in mesh.polygons})
            finally:
                evaluated.to_mesh_clear()
        mats.update(s.material.name for s in obj.material_slots if s.material)
    imgs = images(objects)
    result.update(materials=len(mats), textures=len(imgs), texture_bytes=sum(memory(i) for i in imgs))
    return result

def log(scene, message):
    entry = scene.gr_history.add()
    entry.message = datetime.now(timezone.utc).strftime('%H:%M:%S UTC') + '  ' + message

def safe_name(name):
    return re.sub(r'[^A-Za-z0-9_\-]+', '_', re.sub(r'\.\d{3}$', '', name)).strip('_') or 'Asset'

def unique_path(path):
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    candidate, n = path, 1
    while candidate.exists():
        candidate = path.with_name(f'{path.stem}_{n:03d}{path.suffix}')
        n += 1
    return candidate

@contextmanager
def selected(objects):
    context = bpy.context
    old = list(context.selected_objects)
    active = context.view_layer.objects.active
    if context.mode != 'OBJECT':
        raise ValueError('Switch to Object Mode before preparing assets.')
    for obj in objects:
        if obj.name not in context.view_layer.objects or obj.hide_get() or obj.hide_viewport:
            raise ValueError(f'{obj.name} is hidden or excluded. Make the object visible first.')
    for obj in old:
        obj.select_set(False)
    for obj in objects:
        obj.select_set(True)
    context.view_layer.objects.active = objects[0] if objects else None
    try:
        yield
    finally:
        for obj in context.selected_objects:
            obj.select_set(False)
        for obj in old:
            if obj.name in context.view_layer.objects:
                obj.select_set(True)
        if active and active.name in context.view_layer.objects:
            context.view_layer.objects.active = active

def static_guard(obj):
    if obj.mode != 'OBJECT':
        raise ValueError(f'{obj.name}: return to Object Mode before changing mesh data.')
    if obj.type != 'MESH':
        raise ValueError(f'{obj.name}: this operation requires a mesh.')
    if obj.library or obj.data.library:
        raise ValueError(f'{obj.name}: linked data is read-only; make a local copy first.')
    if obj.data.shape_keys or obj.find_armature() or obj.animation_data:
        raise ValueError(f'{obj.name}: rigged, animated or shape-key mesh requires manual preparation.')

def duplicate(objects, scene):
    collection = bpy.data.collections.new('GameReady_Output')
    scene.collection.children.link(collection)
    mapping = {}
    for obj in objects:
        clone = obj.copy()
        if obj.data:
            clone.data = obj.data.copy()
        collection.objects.link(clone)
        clone['gr_source'] = obj.name
        mapping[obj] = clone
    for old, clone in mapping.items():
        world = old.matrix_world.copy()
        clone.parent = mapping.get(old.parent, old.parent)
        clone.matrix_world = world
        for modifier in clone.modifiers:
            if hasattr(modifier, 'object') and modifier.object in mapping:
                modifier.object = mapping[modifier.object]
        for constraint in clone.constraints:
            if hasattr(constraint, 'target') and constraint.target in mapping:
                constraint.target = mapping[constraint.target]
    return list(mapping.values())

def store_report(scene, data):
    previous = report(scene)
    if previous.get('scope') == data.get('scope'):
        for key in ('before', 'failures'):
            if key in previous and key not in data:
                data[key] = previous[key]
    scene.gr.report_json = json.dumps(data, ensure_ascii=False)
    scene.gr.stale = False

def report(scene):
    return json.loads(scene.gr.report_json or '{}')
