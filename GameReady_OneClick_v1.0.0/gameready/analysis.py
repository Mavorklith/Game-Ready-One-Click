"""Measured checks. Ambiguous topology and authoring intent remain recommendations."""
import bpy
import bmesh
import math
import re
from collections import Counter
from mathutils.kdtree import KDTree
from .core import CATEGORIES, images, memory, stats, store_report

def scan(objects, settings):
    issues = []
    def add(obj, code, category, severity, text, action, fix='', count=1):
        issues.append(dict(object=obj.name if obj else '', code=code, category=category, severity=severity, message=text, recommendation=action, fix=fix, count=int(count)))
    density = {}
    for obj in objects:
        try:
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\-]*', obj.name) or re.search(r'\.\d{3}$', obj.name):
                add(obj, 'NAME', 'Naming', 'WARNING', 'Name has spaces, punctuation or a Blender numeric suffix.', 'Normalize naming with collision-safe unique names.', 'NAME')
            if any(abs(v - 1) > 1e-5 for v in obj.scale) or any(abs(v) > 1e-5 for v in obj.rotation_euler):
                add(obj, 'TRANSFORM', 'Transforms', 'WARNING', 'Rotation or scale is not applied.', 'Review hierarchy, then explicitly apply transforms to static meshes.', 'TRANSFORM')
            if min(obj.scale) < 0:
                add(obj, 'NEGATIVE', 'Transforms', 'WARNING', 'Negative scale can invert winding during export.', 'Inspect mirrored geometry and normals after applying transforms.')
            if any(abs(v) < 1e-4 or abs(v) > 1000 for v in obj.scale):
                add(obj, 'EXTREME_SCALE', 'Transforms', 'ERROR', 'Near-zero or extreme scale.', 'Check intended physical size and scene units.')
            if obj.hide_get() or obj.hide_render:
                add(obj, 'HIDDEN', 'Export', 'INFO', 'Object is hidden in viewport or render.', 'Check export selection explicitly.')
            if obj.constraints or (obj.parent and obj.parent not in objects):
                add(obj, 'HIERARCHY', 'Export', 'WARNING', 'Constraints or a parent outside the scope may affect export.', 'Include dependencies or bake them explicitly.')
            if obj.type == 'EMPTY':
                add(obj, 'EMPTY', 'Export', 'INFO', 'Empty is a hierarchy helper.', 'Retain if needed for hierarchy or attachment points.')
            if obj.type == 'ARMATURE':
                roots = [b for b in obj.data.bones if not b.parent]
                if len(roots) != 1:
                    add(obj, 'ROOTS', 'Export', 'WARNING', f'Armature has {len(roots)} root bones.', 'Review root-motion and target-engine skeleton requirements.')
                unused = sum(not b.use_deform for b in obj.data.bones)
                if unused:
                    add(obj, 'CONTROL_BONES', 'Export', 'INFO', f'{unused} non-deforming bones; not necessarily unused.', 'Review deform-only export and control constraints.')
                suspicious=sum(not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\-]*',b.name) for b in obj.data.bones)
                if suspicious:
                    add(obj,'BONE_NAMES','Naming','INFO',f'{suspicious} bone names contain spaces or punctuation.','Review engine/project naming rules; no rig renaming is automatic.')
                if obj.pose and any(b.constraints for b in obj.pose.bones):
                    add(obj,'POSE_CONSTRAINTS','Export','WARNING','Pose constraints require evaluated/baked animation for transfer.','Test exporter baking and root motion in the target engine.')
            if obj.animation_data:
                add(obj, 'ANIMATION', 'Export', 'INFO', 'Animation is present; automatic cleanup is disabled.', 'Review root motion, key noise and scale/location channels manually.')
            if obj.type != 'MESH':
                continue
            mesh = obj.data
            hidden_vertices=sum(v.hide for v in mesh.vertices)
            if hidden_vertices:
                add(obj,'HIDDEN_GEOMETRY','Geometry','INFO',f'{hidden_vertices} vertices are hidden in Edit Mode.','Hidden edit geometry may still export; unhide and inspect it.')
            if mesh.users > 1:
                add(obj, 'SHARED', 'Geometry', 'INFO', f'Mesh datablock is shared by {mesh.users} users.', 'Copies are made single-user before mesh edits.')
            if not mesh.vertices:
                add(obj, 'EMPTY_MESH', 'Geometry', 'ERROR', 'Mesh has no vertices.', 'Exclude or replace this mesh.')
                continue
            mesh.calc_loop_triangles()
            slivers = 0
            for tri in mesh.loop_triangles:
                a,b,c = [mesh.vertices[i].co for i in tri.vertices]
                lengths = ((a-b).length_squared,(b-c).length_squared,(c-a).length_squared)
                area = (b-a).cross(c-a).length / 2
                if area > 1e-15 and max(lengths) / (2*area) > 100:
                    slivers += 1
            if slivers:
                add(obj,'SLIVERS','Geometry','INFO',f'{slivers} triangles have length/altitude ratio above 100.','Inspect sliver triangles around UV seams and narrow bevels.')
            if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_\-]*',mesh.name):
                add(obj,'MESH_NAME','Naming','INFO','Mesh datablock name contains export-unfriendly characters.','Normalize object naming to synchronize its mesh name.','NAME')
            if len(mesh.loop_triangles) > settings.triangle_budget:
                add(obj, 'TRIANGLES', 'Performance', 'WARNING', f'{len(mesh.loop_triangles):,} base triangles exceed the {settings.triangle_budget:,} target.', 'Enable budget reduction after reviewing silhouette.')
            ngons = sum(len(p.vertices) > 4 for p in mesh.polygons)
            if ngons:
                add(obj, 'NGONS', 'Geometry', 'INFO', f'{ngons} ngons; acceptable for some static surfaces.', 'Preview triangulation if shading or deformation is sensitive.')
            if mesh.has_custom_normals:
                add(obj, 'CUSTOM_NORMALS', 'Geometry', 'INFO', 'Custom normals are present.', 'Preserve authored normals; inspect after topology changes.')
            bm = bmesh.new()
            try:
                bm.from_mesh(mesh)
                bm.verts.ensure_lookup_table()
                boundary = sum(e.is_boundary for e in bm.edges)
                nonmanifold = sum(not e.is_manifold and not e.is_boundary and bool(e.link_faces) for e in bm.edges)
                if boundary:
                    add(obj, 'BOUNDARY', 'Geometry', 'WARNING', f'{boundary} boundary edges; open surfaces may be intentional.', 'Check whether this asset should be watertight.')
                if nonmanifold:
                    add(obj, 'NONMANIFOLD', 'Geometry', 'ERROR', f'{nonmanifold} non-manifold interior edges.', 'Repair junctions manually before collision or volume workflows.')
                loose = sum(not v.link_faces for v in bm.verts)
                if loose:
                    add(obj, 'LOOSE', 'Geometry', 'WARNING', f'{loose} vertices are not part of a face.', 'Remove loose geometry only if not used as helpers.', 'LOOSE', loose)
                zero_faces = sum(f.calc_area() < settings.merge_distance ** 2 for f in bm.faces)
                zero_edges = sum(e.calc_length() < settings.merge_distance for e in bm.edges)
                if zero_faces or zero_edges:
                    add(obj, 'DEGENERATE', 'Geometry', 'WARNING', f'{zero_faces} tiny/zero-area faces; {zero_edges} short/zero-length edges.', 'Inspect scale, then optionally dissolve degenerate geometry.', 'DEGENERATE', zero_faces + zero_edges)
                bad = sum(e.is_manifold and not e.is_contiguous for e in bm.edges)
                if bad:
                    add(obj, 'NORMALS', 'Geometry', 'WARNING', f'{bad} edges have inconsistent face winding.', 'Recalculate connected face normals after checking intended sides.', 'NORMALS', bad)
                if bm.faces and all(e.is_manifold for e in bm.edges) and bm.calc_volume(signed=True) < 0:
                    add(obj, 'INVERTED', 'Geometry', 'WARNING', 'Closed mesh has negative signed volume.', 'Review inward-facing shells; recalculate normals if unintended.', 'NORMALS')
                kd = KDTree(len(bm.verts))
                for v in bm.verts:
                    kd.insert(v.co, v.index)
                kd.balance()
                near = sum(any(j < v.index for _, j, _ in kd.find_range(v.co, settings.merge_distance)) for v in bm.verts)
                if near:
                    add(obj, 'DUPLICATE_VERTS', 'Geometry', 'WARNING', f'{near} vertices have a nearby predecessor within tolerance.', 'Seams may be intentional. Merge only after review.', 'MERGE', near)
                seen, components = set(), 0
                for v in bm.verts:
                    if v.index in seen:
                        continue
                    components += 1
                    stack = [v]
                    seen.add(v.index)
                    while stack:
                        current = stack.pop()
                        for e in current.link_edges:
                            other = e.other_vert(current)
                            if other.index not in seen:
                                seen.add(other.index)
                                stack.append(other)
                if components > 8:
                    add(obj, 'FRAGMENTS', 'Geometry', 'WARNING', f'{components} disconnected components.', 'AI rescue: inspect floating parts; intentional parts are never removed automatically.')
                faces = Counter(tuple(sorted(tuple(round(c, 7) for c in v.co) for v in f.verts)) for f in bm.faces)
                overlaps = sum(n - 1 for n in faces.values() if n > 1)
                if overlaps:
                    add(obj, 'OVERLAPPING_FACES', 'Geometry', 'WARNING', f'{overlaps} coincident faces at rounded coordinate precision.', 'Inspect duplicate surfaces manually; internal intersections are not exhaustively tested.')
            finally:
                bm.free()
            if max(obj.dimensions) < 0.001:
                add(obj, 'TINY', 'Transforms', 'WARNING', 'Object is smaller than 0.001 scene units.', 'Verify units and intended size.')
            if (obj.matrix_world.translation - sum((obj.matrix_world @ v.co for v in mesh.vertices), obj.location * 0) / len(mesh.vertices)).length > max(obj.dimensions) * 2:
                add(obj, 'PIVOT', 'Transforms', 'INFO', 'Origin lies far from the geometry center.', 'Review pivot preview before repositioning.')
            if obj.modifiers:
                add(obj, 'MODIFIERS', 'Export', 'INFO', 'Modifier stack: ' + ', '.join(m.type for m in obj.modifiers), 'Statistics use evaluated geometry. Inspect exporter modifier behavior.')
            uv = mesh.uv_layers.active
            if not uv:
                add(obj, 'NO_UV', 'UVs', 'WARNING', 'No UV map.', 'Create a UV unwrap if textured export requires one.')
            else:
                coords = [d.uv for d in uv.data]
                areas, ratios, exact_uv = [], [], Counter()
                for tri in mesh.loop_triangles:
                    a, b, c = [coords[i] for i in tri.loops]
                    area = abs((b.x-a.x)*(c.y-a.y)-(b.y-a.y)*(c.x-a.x))/2
                    areas.append(area)
                    va, vb, vc = [obj.matrix_world @ mesh.vertices[i].co for i in tri.vertices]
                    world_area = (vb-va).cross(vc-va).length / 2
                    if world_area > 1e-15:
                        ratios.append(area/world_area)
                    exact_uv[tuple(sorted((round(p.x, 6), round(p.y, 6)) for p in (a,b,c)))] += 1
                if not areas or sum(areas) < 1e-12:
                    add(obj, 'EMPTY_UV', 'UVs', 'ERROR', 'Active UV map has no measurable area.', 'Unwrap the mesh.')
                elif ratios:
                    density[obj.name] = settings.density_resolution * math.sqrt(sum(areas) / max(sum((obj.matrix_world @ mesh.vertices[t.vertices[1]].co - obj.matrix_world @ mesh.vertices[t.vertices[0]].co).cross(obj.matrix_world @ mesh.vertices[t.vertices[2]].co - obj.matrix_world @ mesh.vertices[t.vertices[0]].co).length/2 for t in mesh.loop_triangles), 1e-15))
                    valid = [r for r in ratios if r > 1e-12]
                    if valid and max(valid) / min(valid) > 16:
                        add(obj, 'UV_DENSITY', 'UVs', 'WARNING', 'Per-triangle UV/world area ratios vary by more than 16x.', 'Inspect stretching and intentionally varied density.')
                if any(p.x < 0 or p.y < 0 or p.x > 1 or p.y > 1 for p in coords):
                    add(obj, 'UV_RANGE', 'UVs', 'INFO', 'UVs extend outside 0–1.', 'Valid for tiled/UDIM workflows; verify engine setup.')
                overlap = sum(n-1 for n in exact_uv.values() if n > 1)
                if overlap:
                    add(obj, 'UV_STACKED', 'UVs', 'INFO', f'{overlap} exactly stacked UV triangles (quantized test).', 'Intentional mirrored/stacked UVs are retained. Partial overlaps require manual UV inspection.')
                tiny = sum(a < 1 / settings.density_resolution ** 2 for a in areas)
                if tiny:
                    add(obj, 'UV_TINY', 'UVs', 'INFO', f'{tiny} UV triangles occupy less than one reference pixel.', 'Inspect tiny islands at the reference resolution.')
                if sum(areas) < .2 and areas:
                    add(obj, 'UV_UTILIZATION', 'UVs', 'INFO', f'Summed UV triangle area is {sum(areas):.3f}; not unique coverage.', 'Inspect packing; overlapping/tiled UVs affect interpretation.')
            if len(mesh.uv_layers) > 2:
                add(obj, 'UV_COUNT', 'UVs', 'INFO', f'{len(mesh.uv_layers)} UV maps.', 'Confirm all channels are required.')
            if any(re.search(r'\.\d{3}$', layer.name) for layer in mesh.uv_layers):
                add(obj,'UV_NAMES','UVs','INFO','UV map names contain Blender duplicate suffixes.','Confirm which layer is active for rendering and export.')
            used = {p.material_index for p in mesh.polygons}
            unused = sum(i not in used for i in range(len(obj.material_slots)))
            empty = sum(not s.material for s in obj.material_slots)
            if unused:
                add(obj, 'UNUSED_SLOTS', 'Materials', 'WARNING', f'{unused} unused material slots.', 'Remove unused slots and remap face assignments.', 'SLOTS', unused)
            if empty:
                add(obj, 'EMPTY_SLOTS', 'Materials', 'WARNING', f'{empty} empty material slots.', 'Assign required materials; empty used slots are not deleted.')
            if len(used) > settings.max_materials:
                add(obj, 'MATERIAL_BUDGET', 'Performance', 'WARNING', f'{len(used)} used material indices exceed target.', 'Atlas/bake materials manually to reduce draw-call-related slots.')
            for slot in obj.material_slots:
                mat = slot.material
                if mat and mat.node_tree:
                    nodes = mat.node_tree.nodes
                    if len(nodes)>30:
                        add(obj,'COMPLEX_MATERIAL','Materials','WARNING',f'{mat.name}: {len(nodes)} shader nodes.','Review baking and runtime shader cost; Blender node count is only a complexity indicator.')
                    if re.search(r'\.\d{3}$',mat.name):
                        add(obj,'MATERIAL_COPY','Materials','INFO',f'{mat.name}: duplicate-style material name.','Compare graphs before consolidating; matching names do not prove equivalence.')
                    if any(n.type not in {'BSDF_PRINCIPLED','TEX_IMAGE','OUTPUT_MATERIAL','NORMAL_MAP','REROUTE','UVMAP','TEX_COORD','MAPPING'} for n in nodes):
                        add(obj, 'MATERIAL_GRAPH', 'Materials', 'WARNING', f'{mat.name}: shader graph may require baking.', 'Exporters do not translate arbitrary procedural Blender graphs.')
        except Exception as exc:
            add(obj, 'SCAN_FAILED', 'Export', 'ERROR', f'Analysis could not finish: {exc}', 'Resolve this object before relying on the report.')
    texture_rows = []
    paths = {}
    for image in images(objects):
        w, h = image.size
        name = image.name.lower()
        kind = next((k for k in ('normal', 'roughness', 'metallic', 'ao', 'basecolor') if k in name), 'unknown')
        warnings = []
        if image.source=='FILE' and image.filepath:
            resolved=bpy.path.abspath(image.filepath,library=image.library)
            if resolved in paths:
                add(None,'DUPLICATE_IMAGE','Textures','INFO',f'{image.name} and {paths[resolved]} reference the same external path.','Review color-space and alpha differences before consolidating.')
            else: paths[resolved]=image.name
        if image.source == 'FILE' and not image.packed_file and not __import__('os').path.isfile(bpy.path.abspath(image.filepath, library=image.library)):
            warnings.append('Missing external file')
            add(None, 'MISSING_IMAGE', 'Textures', 'ERROR', image.name + ': external file missing.', 'Relink or pack the correct image.')
        if not w or not h:
            warnings.append('No pixel data')
        if max(w,h) > settings.max_texture:
            warnings.append('Over resolution target')
        if w and h and (w & (w-1) or h & (h-1)):
            warnings.append('Non-power-of-two')
        if min(w,h) and max(w,h)/min(w,h) > 4:
            warnings.append('Unusual aspect ratio')
        if 0 < max(w,h) < 128:
            warnings.append('Small image; may be intentional')
        if kind in ('normal', 'roughness', 'metallic', 'ao') and image.colorspace_settings.name not in ('Non-Color','Raw'):
            warnings.append('Data map uses color transform')
        if warnings:
            add(None, 'TEXTURE_REVIEW', 'Textures', 'WARNING', image.name + ': ' + '; '.join(warnings), 'Review texture dashboard; resizing is opt-in.')
        texture_rows.append(dict(name=image.name, kind=kind, width=w, height=h, format=image.file_format, depth=image.depth, channels=image.channels, alpha_mode=image.alpha_mode, colorspace=image.colorspace_settings.name, packed=bool(image.packed_file or image.packed_files), users=image.users, bytes=memory(image), warning='; '.join(warnings)))
    unused=sum(i.users==0 for i in bpy.data.images)
    if unused:
        add(None,'UNUSED_IMAGES','Textures','INFO',f'{unused} zero-user image datablocks exist in the blend file (global inventory).','Only explicitly selected zero-user images can be removed.')
    gray=[r for r in texture_rows if r['kind'] in {'roughness','metallic','ao'}]
    if len(gray)>=2:
        add(None,'PACKING_OPPORTUNITY','Textures','INFO','Multiple scalar-map candidates may be packed into RGBA channels.','Confirm data-map roles and import channel assignments before packing.')
    if len(objects)>100:
        add(None,'OBJECT_FRAGMENTATION','Performance','WARNING',f'{len(objects)} objects in this scope.','Inspect asset boundaries before combining any geometry.')
    if sum(o.type=='ARMATURE' for o in objects)>1:
        add(None,'MULTIPLE_RIGS','Export','WARNING','Multiple armatures in export scope.','Verify intended skeleton boundaries in the target engine.')
    if settings.engine=='UNREAL':
        for obj in objects:
            if obj.type=='MESH' and not obj.name.startswith(('SM_','SK_')):
                add(obj,'UNREAL_NAME','Naming','INFO','Unreal-oriented convention recommends SM_ or SK_ prefixes.','This is a project convention, not an engine requirement.')
    measured = stats(objects)
    if measured['triangles'] > settings.triangle_budget:
        add(None, 'SCOPE_BUDGET', 'Performance', 'WARNING', f"Scope has {measured['triangles']:,} evaluated triangles.", 'The triangle budget applies to the entire selected scope.')
    if measured['texture_bytes'] > settings.texture_budget_mb * 1024**2:
        add(None, 'TEXTURE_MEMORY', 'Performance', 'WARNING', 'Estimated uncompressed texture memory exceeds budget.', 'Review image size, channel packing and runtime compression.')
    # Each category/object/check contributes once; INFO never reduces score.
    penalties = {'ERROR': 20, 'WARNING': 5, 'INFO': 0}
    scores = {c: max(0, 100-sum(penalties[i['severity']] for i in {(x['object'],x['code']):x for x in issues if x['category']==c}.values())) for c in CATEGORIES}
    from datetime import datetime,timezone
    return dict(schema=1, scanned_at=datetime.now(timezone.utc).isoformat(), blender=bpy.app.version_string, score=round(sum(scores.values())/len(scores)), categories=scores, issues=issues, statistics=measured, textures=texture_rows, density=density, engine=settings.engine, scope=[o.name for o in objects], limitations=['Base-mesh topology checks; evaluated statistics.', 'UV overlap detects exactly stacked triangles only.', 'Internal intersections, rig key noise and shader equivalence require manual review.', 'Texture memory is uncompressed with mipmaps; not measured GPU allocation.', 'Readiness is Blender-side only; no engine import certification.'])

def refresh(context, objects=None):
    result = scan(objects if objects is not None else __import__(__package__ + '.core', fromlist=['scope']).scope(context), context.scene.gr)
    context.scene.gr_issues.clear()
    for issue in result['issues']:
        row = context.scene.gr_issues.add()
        for key in ('object','code','category','severity','message','recommendation','fix'):
            setattr(row, key, issue[key])
    store_report(context.scene, result)
    return result
