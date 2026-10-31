"""Explicit static-asset operations. No rig, animation or shape-key cleanup."""
import bpy
import bmesh
import math
import re
from mathutils import Vector, Matrix
from .core import static_guard, selected, duplicate, safe_name, log, stats

def fix(obj, action, settings):
    if action == 'NAME':
        if obj.library:
            raise ValueError('Linked object cannot be renamed.')
        base = safe_name(obj.get('gr_source', obj.name))
        if settings.rename_pattern:
            base = re.sub(settings.rename_pattern, settings.rename_replace, base)
        prefix = settings.prefix
        obj.name = (prefix if not base.startswith(prefix) else '') + base + settings.suffix
        if obj.type == 'MESH':
            if obj.data.users > 1:
                obj.data = obj.data.copy()
            obj.data.name = obj.name
        return
    static_guard(obj)
    if action == 'TRANSFORM':
        if obj.parent or obj.children or obj.constraints or obj.modifiers:
            raise ValueError('Transform application skipped: hierarchy, constraints or modifiers need manual review.')
        if min(abs(v) for v in obj.scale) < 1e-6:
            raise ValueError('Near-zero scale cannot be safely applied.')
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        with selected([obj]):
            bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
        return
    if obj.data.users > 1:
        obj.data = obj.data.copy()
    if action == 'SLOTS':
        if any(s.link == 'OBJECT' for s in obj.material_slots):
            raise ValueError('Object-linked material slots require manual review.')
        used = {p.material_index for p in obj.data.polygons}
        for index in reversed(range(len(obj.data.materials))):
            if index not in used:
                obj.data.materials.pop(index=index)
        return
    if obj.modifiers:
        raise ValueError('Topology cleanup skipped on modifier-driven mesh. Prepare the stack manually.')
    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        if action == 'LOOSE':
            bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context='VERTS')
        elif action == 'NORMALS':
            bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
        elif action == 'MERGE':
            bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=settings.merge_distance)
        elif action == 'DEGENERATE':
            bmesh.ops.dissolve_degenerate(bm, edges=list(bm.edges), dist=settings.merge_distance)
        else:
            raise ValueError('Unsupported fix: ' + action)
        bm.to_mesh(obj.data)
        obj.data.update()
    finally:
        bm.free()

def workflow(context, actions=None):
    from .core import scope
    from .analysis import scan, refresh
    settings = context.scene.gr
    if context.mode != 'OBJECT':
        raise ValueError('Return to Object Mode before running Make Game Ready.')
    sources = scope(context)
    if not sources:
        raise ValueError('Select at least one asset before preparing it.')
    before = stats(sources)
    if settings.preserve or settings.level == 'AGGRESSIVE':
        targets = duplicate(sources, context.scene)
    else:
        targets = sources
    allowed = actions if actions is not None else ['SLOTS'] + (['NAME'] if settings.fix_names else []) + (['TRANSFORM'] if settings.fix_transforms else [])
    if actions is None and settings.level != 'SAFE':
        allowed += ['LOOSE','NORMALS','DEGENERATE']
    if actions is None and settings.level == 'AGGRESSIVE':
        allowed += ['MERGE']
    failures = []
    wm = context.window_manager
    wm.progress_begin(0, max(len(targets), 1))
    try:
        for index, obj in enumerate(targets):
            wm.progress_update(index)
            findings = scan([obj], settings)['issues']
            needed = {i['fix'] for i in findings if i['fix']}
            for action in allowed:
                if action not in needed:
                    continue
                try:
                    fix(obj, action, settings)
                    remaining = [i for i in scan([obj], settings)['issues'] if i['fix'] == action]
                    log(context.scene, f'{obj.name}: {action} ' + ('still needs review after operation' if remaining else 'verified clear by rescan'))
                except Exception as exc:
                    failures.append(f'{obj.name}: {action}: {exc}')
                    log(context.scene, failures[-1])
        if settings.optimize:
            optimize(targets, settings, context.scene)
        if settings.make_lods:
            for obj in targets:
                if obj.type == 'MESH':
                    try:
                        lods(obj, settings)
                    except Exception as exc:
                        failures.append(f'LOD {obj.name}: {exc}')
        if settings.make_colliders:
            for obj in targets:
                if obj.type == 'MESH':
                    try:
                        collider(obj, settings)
                    except Exception as exc:
                        failures.append(f'Collider {obj.name}: {exc}')
    finally:
        wm.progress_end()
    for obj in context.selected_objects:
        obj.select_set(False)
    for obj in targets:
        obj.select_set(True)
    context.view_layer.objects.active = targets[0]
    if targets != sources and settings.original_action != 'KEEP':
        for obj in sources:
            obj.hide_set(True)
            if settings.original_action == 'ARCHIVE':
                obj['gr_backup'] = True
                obj.hide_render = True
    settings.scope = 'SELECTED'
    result = refresh(context, targets)
    result['before'] = before
    result['failures'] = failures
    from .core import store_report
    store_report(context.scene, result)
    log(context.scene, f'Workflow complete: {len(targets)} objects, {len(failures)} skipped/failed operations. Final issues: {len(result["issues"])}.')
    return result

def optimize(objects, settings, scene):
    eligible = []
    for obj in objects:
        if obj.type != 'MESH':
            continue
        try:
            static_guard(obj)
            if obj.modifiers:
                raise ValueError('Existing modifiers require manual stack review.')
            eligible.append(obj)
        except ValueError as exc:
            log(scene, f'Reduction skipped: {exc}')
    total = stats(objects)['triangles']
    ratio = min(1.0, settings.triangle_budget / max(total, 1))
    if ratio < 1:
        for obj in eligible:
            mod = obj.modifiers.new('GameReady Budget Reduction', 'DECIMATE')
            mod.ratio = max(.001, ratio)
            mod.use_collapse_triangulate = True
        bpy.context.view_layer.update()
        log(scene, f'Non-destructive reduction modifiers added; measured result {stats(objects)["triangles"]:,} triangles. Target is approximate.')

def collection_for(obj):
    return obj.users_collection[0] if obj.users_collection else bpy.context.scene.collection

def evaluated_copy(obj, name):
    static_guard(obj)
    mesh = bpy.data.meshes.new_from_object(obj.evaluated_get(bpy.context.evaluated_depsgraph_get()), preserve_all_data_layers=True, depsgraph=bpy.context.evaluated_depsgraph_get())
    clone = bpy.data.objects.new(name, mesh)
    collection_for(obj).objects.link(clone)
    clone.matrix_world = obj.matrix_world.copy()
    return clone

def lods(obj, settings):
    static_guard(obj)
    if any(o.get('gr_lod_source') == obj.name for o in bpy.context.scene.objects):
        raise ValueError('A LOD chain already exists for this object; remove or rename that chain explicitly before regenerating.')
    chain = []
    ratios = [1, settings.lod1, settings.lod2, settings.lod3, settings.lod4]
    if any(ratios[i] >= ratios[i-1] for i in range(1, settings.lod_count+1)):
        raise ValueError('LOD ratios must decrease at each level.')
    for level in range(settings.lod_count + 1):
        clone = evaluated_copy(obj, f'{safe_name(obj.name)}_LOD{level}')
        clone['gr_helper'] = 'LOD'
        clone['gr_lod_source'] = obj.name
        clone['gr_lod_level'] = level
        if level:
            mod = clone.modifiers.new('GameReady LOD Reduction', 'DECIMATE')
            mod.ratio = ratios[level]
            mod.use_collapse_triangulate = True
        clone.hide_set(True)
        clone.hide_render = True
        chain.append(clone)
    return chain

def bounds(obj):
    evaluated = obj.evaluated_get(bpy.context.evaluated_depsgraph_get())
    points = [obj.matrix_world @ Vector(corner) for corner in evaluated.bound_box]
    return Vector([min(p[i] for p in points) for i in range(3)]), Vector([max(p[i] for p in points) for i in range(3)])

def collider(obj, settings):
    static_guard(obj)
    lo, hi = bounds(obj)
    center, size = (lo+hi)/2, hi-lo
    mode = settings.collider_mode
    if mode == 'SMART':
        mode = 'SPHERE' if max(size)/max(min(size), 1e-8) < 1.15 else 'CAPSULE' if size.z > max(size.x,size.y)*2 else 'BOX'
    if mode in {'HULL','MESH','COMPOUND'}:
        base = evaluated_copy(obj, 'Collision')
        if mode == 'MESH':
            generated = [base]
        else:
            bm = bmesh.new()
            bm.from_mesh(base.data)
            try:
                groups = []
                if mode == 'COMPOUND':
                    remaining = set(bm.verts)
                    while remaining:
                        seed = remaining.pop()
                        group, stack = [seed], [seed]
                        while stack:
                            for e in stack.pop().link_edges:
                                for v in e.verts:
                                    if v in remaining:
                                        remaining.remove(v)
                                        group.append(v)
                                        stack.append(v)
                        groups.append(group)
                    if len(groups) > 64:
                        raise ValueError('More than 64 components; select a simpler collision strategy.')
                else:
                    groups = [list(bm.verts)]
                generated = []
                for group in groups:
                    if len(group) < 4:
                        continue
                    hull = bmesh.new()
                    try:
                        for v in group:
                            hull.verts.new(v.co)
                        result = bmesh.ops.convex_hull(hull, input=list(hull.verts), use_existing_faces=False)
                        trash = [g for key in ('geom_interior','geom_unused') for g in result.get(key, []) if isinstance(g, bmesh.types.BMVert) and g.is_valid]
                        if trash:
                            bmesh.ops.delete(hull, geom=list(set(trash)), context='VERTS')
                        mesh = bpy.data.meshes.new('CollisionHull')
                        hull.to_mesh(mesh)
                        helper = bpy.data.objects.new('CollisionHull', mesh)
                        collection_for(obj).objects.link(helper)
                        helper.matrix_world = obj.matrix_world.copy()
                        generated.append(helper)
                    finally:
                        hull.free()
            finally:
                bm.free()
                mesh = base.data
                bpy.data.objects.remove(base, do_unlink=True)
                if mesh.users == 0:
                    bpy.data.meshes.remove(mesh)
    else:
        mesh = bpy.data.meshes.new('CollisionPrimitive')
        bm = bmesh.new()
        try:
            if mode == 'BOX':
                bmesh.ops.create_cube(bm, size=1)
                for v in bm.verts:
                    v.co = Vector([v.co[i]*size[i] for i in range(3)])
            else:
                radius = size.length/2 if mode == 'SPHERE' else math.hypot(size.x,size.y)/2
                bmesh.ops.create_uvsphere(bm, u_segments=20, v_segments=12, radius=max(radius, .0001))
                if mode == 'CAPSULE':
                    offset = size.z/2
                    for v in bm.verts:
                        v.co.z += offset if v.co.z >= 0 else -offset
            bm.to_mesh(mesh)
        finally:
            bm.free()
        helper = bpy.data.objects.new('CollisionPrimitive', mesh)
        collection_for(obj).objects.link(helper)
        helper.location = center
        generated = [helper]
    if not generated:
        raise ValueError('No volumetric collider could be generated from this mesh.')
    prefix = ('UBX' if mode=='BOX' else 'USP' if mode=='SPHERE' else 'UCP' if mode=='CAPSULE' else 'UCX') if settings.engine=='UNREAL' and mode!='MESH' else 'COL'
    for index, helper in enumerate(generated):
        helper.name = f'{prefix}_{obj.name}_{index:02d}'
        helper['gr_helper'] = 'COLLIDER'
        helper['gr_collision_source'] = obj.name
        helper.display_type = 'WIRE'
        helper.hide_render = True
    return generated

def pivot_position(obj, mode, context):
    lo, hi = bounds(obj)
    point = (lo+hi)/2
    if mode in {'BOTTOM','GROUND'}: point.z = lo.z
    elif mode == 'TOP': point.z = hi.z
    elif mode in {'LEFT','DOOR'}: point.x = lo.x
    elif mode == 'RIGHT': point.x = hi.x
    elif mode in {'FRONT','WALL'}: point.y = lo.y
    elif mode == 'BACK': point.y = hi.y
    elif mode == 'WORLD': point = Vector((0,0,0))
    elif mode == 'CURSOR': point = context.scene.cursor.location.copy()
    elif mode == 'GEOMETRY': point = sum((obj.matrix_world @ v.co for v in obj.data.vertices), Vector()) / max(len(obj.data.vertices),1)
    elif mode in {'VERTEX','FACE'}:
        obj.update_from_editmode()
        values = [obj.matrix_world @ v.co for v in obj.data.vertices if v.select] if mode == 'VERTEX' else [obj.matrix_world @ p.center for p in obj.data.polygons if p.select]
        if not values: raise ValueError('Select mesh components in Edit Mode, then return to Object Mode.')
        point = sum(values, Vector()) / len(values)
    return point

def set_pivot(obj, point):
    static_guard(obj)
    if obj.constraints or obj.parent or obj.children or obj.modifiers:
        raise ValueError('Pivot changes on hierarchies, constraints or modifiers require manual review.')
    if obj.data.users > 1: obj.data = obj.data.copy()
    old = obj.matrix_world.copy()
    new = old.copy()
    new.translation = point
    obj.data.transform(new.inverted() @ old)
    obj.matrix_world = new
    obj.data.update()
