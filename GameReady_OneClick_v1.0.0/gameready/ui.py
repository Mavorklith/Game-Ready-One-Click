"""Blender-native task panels, issue review and explicit operation previews."""
import bpy
import json
import traceback
from pathlib import Path
from bpy.props import BoolProperty, StringProperty, IntProperty, FloatProperty, EnumProperty, PointerProperty, CollectionProperty
from bpy.app.handlers import persistent
from . import core, analysis, operations, textures, exporting

def items(values):
    return [(v, v.replace('_',' ').title(), '') for v in values]

class GRSettings(bpy.types.PropertyGroup):
    scope: EnumProperty(name='Asset scope', items=items(['SELECTED','COLLECTION','SCENE']))
    tab: EnumProperty(name='Workspace', items=items(['DASHBOARD','SCAN','FIX','OPTIMIZE','LOD','COLLIDERS','TEXTURES','EXPORT','REPORT']))
    level: EnumProperty(name='Cleanup level', items=[('SAFE','Safe','Remove unused slots; optional naming/transforms only'),('BALANCED','Balanced','Also remove loose geometry, dissolve degenerates and recalculate normals'),('AGGRESSIVE','Aggressive','Also merge nearby vertices. Always duplicate originals')])
    preserve: BoolProperty(name='Preserve Original', default=True, description='Prepare separate objects in a new GameReady_Output collection')
    original_action: EnumProperty(name='Original after preparation', items=items(['KEEP','HIDE','ARCHIVE']))
    fix_names: BoolProperty(name='Normalize names', default=True)
    fix_transforms: BoolProperty(name='Apply static transforms', default=False, description='Only meshes without hierarchy, animation, shape keys, constraints or modifiers')
    merge_distance: FloatProperty(name='Merge / degeneracy tolerance', default=.00001, min=.00000001, max=.1, precision=6, description='Local mesh units; close seams may be intentional')
    triangle_budget: IntProperty(name='Scope triangle budget', default=50000, min=4)
    texture_budget_mb: IntProperty(name='Texture budget (MiB)', default=128, min=1)
    max_materials: IntProperty(name='Material target per object', default=4, min=1)
    max_texture: IntProperty(name='Max texture dimension', default=2048, min=16, max=16384)
    optimize: BoolProperty(name='Add budget reduction modifiers', default=False, description='Opt-in decimation; visually inspect silhouette and UVs afterwards')
    make_lods: BoolProperty(name='Generate LOD chain', default=False)
    make_colliders: BoolProperty(name='Generate collision helpers', default=False)
    lod_count: IntProperty(name='Reduced levels', default=3, min=1, max=4)
    lod1: FloatProperty(name='LOD1 ratio', default=.6, min=.001, max=.999)
    lod2: FloatProperty(name='LOD2 ratio', default=.35, min=.001, max=.999)
    lod3: FloatProperty(name='LOD3 ratio', default=.18, min=.001, max=.999)
    lod4: FloatProperty(name='LOD4 ratio', default=.08, min=.001, max=.999)
    lod_preview: IntProperty(name='Preview level (-1 = source)', default=-1, min=-1, max=4)
    collider_mode: EnumProperty(name='Strategy', items=items(['SMART','BOX','SPHERE','CAPSULE','HULL','MESH','COMPOUND']))
    pivot_mode: EnumProperty(name='Pivot position', items=items(['CENTER','GEOMETRY','BOTTOM','TOP','LEFT','RIGHT','FRONT','BACK','WORLD','CURSOR','VERTEX','FACE','DOOR','WHEEL','GROUND','WALL']))
    prefix: StringProperty(name='Object prefix', default='SM_')
    suffix: StringProperty(name='Object suffix', default='')
    rename_pattern: StringProperty(name='Regex pattern', default='')
    rename_replace: StringProperty(name='Replacement', default='')
    density_resolution: IntProperty(name='Reference resolution', default=2048, min=16, max=16384)
    density_target: FloatProperty(name='Pixels per scene unit', default=512, min=.01)
    output_dir: StringProperty(name='Output folder', subtype='DIR_PATH', default='//GameReady_Exports/', options={'PATH_SUPPORTS_BLEND_RELATIVE'})
    export_name: StringProperty(name='File name', default='GameReady_Asset')
    export_format: EnumProperty(name='Format', items=items(['GLB','FBX','OBJ']))
    engine: EnumProperty(name='Engine profile', items=items(['GENERIC','UNITY','UNREAL','GODOT']))
    axis_forward: EnumProperty(name='Forward', items=[(v,v,'') for v in ('-Z','-Y','X','Y','Z','-X')], default='-Z')
    axis_up: EnumProperty(name='Up', items=[(v,v,'') for v in ('Y','Z','X','-X','-Y','-Z')], default='Y')
    export_scale: FloatProperty(name='Export scale', default=1, min=.00001)
    export_animation: BoolProperty(name='Include animation', default=False)
    export_armatures: BoolProperty(name='Include armatures / skins', default=True)
    export_materials: BoolProperty(name='Export materials', default=True)
    embed_textures: BoolProperty(name='Embed FBX textures', default=True)
    triangulate: BoolProperty(name='Triangulate at export', default=True)
    export_lods: BoolProperty(name='Include generated LODs', default=False)
    export_colliders: BoolProperty(name='Include colliders', default=False)
    override_errors: BoolProperty(name='Override critical validation issues', default=False, description='Export despite ERROR findings. Engine import may fail')
    profile: EnumProperty(name='Production preset', items=items(['MOBILE','PC','VR','WEB','UNITY_MOBILE','UNITY_PC','UNREAL_PROP','AI_RESCUE','CUSTOM']))
    profile_name: StringProperty(name='Saved profile name', default='My GameReady Preset')
    image_name: StringProperty(name='Image')
    image_format: EnumProperty(name='Copy format', items=items(['PNG','OPEN_EXR']))
    relink_path: StringProperty(name='Replacement image file', subtype='FILE_PATH', options={'PATH_SUPPORTS_BLEND_RELATIVE'})
    pack_r: StringProperty(name='R (AO)')
    pack_g: StringProperty(name='G (Roughness)')
    pack_b: StringProperty(name='B (Metallic)')
    pack_a: StringProperty(name='A (optional)')
    pack_resolution: IntProperty(name='Packed resolution', default=1024, min=16, max=2048)
    report_json: StringProperty(options={'HIDDEN'})
    stale: BoolProperty(default=True, options={'HIDDEN'})

class GRIssue(bpy.types.PropertyGroup):
    object: StringProperty()
    code: StringProperty()
    category: StringProperty()
    severity: StringProperty()
    message: StringProperty()
    recommendation: StringProperty()
    fix: StringProperty()

class GRHistory(bpy.types.PropertyGroup):
    message: StringProperty()

class GRProfile(bpy.types.PropertyGroup):
    name: StringProperty()
    data: StringProperty()

def require_objects(context):
    objects = core.scope(context)
    if not objects:
        raise ValueError('No assets in the chosen scope. Select an object or change scope.')
    return objects

class GR_OT_scan(bpy.types.Operator):
    bl_idname = 'gr.scan'
    bl_label = 'Scan Asset'
    bl_description = 'Measure current scene data and refresh the issue list; no mesh changes'
    def execute(self, context):
        try:
            result = analysis.refresh(context, require_objects(context))
            self.report({'INFO'}, f'Readiness {result["score"]}/100; {len(result["issues"])} findings')
            return {'FINISHED'}
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}

class GR_OT_prepare(bpy.types.Operator):
    bl_idname = 'gr.prepare'
    bl_label = 'MAKE GAME READY'
    bl_description = 'Review enabled operations, then prepare the chosen scope and verify with a fresh scan'
    bl_options = {'REGISTER','UNDO'}
    def invoke(self, context, event):
        try:
            self.count = len(require_objects(context))
            self.triangles = core.stats(core.scope(context))['triangles']
        except Exception as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=480)
    def draw(self, context):
        s = context.scene.gr
        layout = self.layout
        layout.label(text=f'{self.count} objects | {self.triangles:,} evaluated triangles', icon='MESH_DATA')
        layout.label(text='Duplicate originals' if s.preserve or s.level=='AGGRESSIVE' else 'Modify selected originals (Undo available)', icon='DUPLICATE')
        layout.label(text='Remove unused material slots')
        if s.fix_names: layout.label(text='Normalize object and mesh names')
        if s.fix_transforms: layout.label(text='Apply transforms on eligible static meshes')
        if s.level!='SAFE': layout.label(text='CHANGES TOPOLOGY: loose/degenerate cleanup + normals', icon='ERROR')
        if s.level=='AGGRESSIVE': layout.label(text=f'Merge within {s.merge_distance:g}; seams may change', icon='ERROR')
        if s.optimize: layout.label(text=f'Decimation target: {s.triangle_budget:,} scope triangles (approximate)')
        if s.make_lods: layout.label(text=f'Generate LOD0 through LOD{s.lod_count}')
        if s.make_colliders: layout.label(text='Generate ' + s.collider_mode + ' collision helpers')
        layout.label(text='Rescan output; skipped operations remain in the session log.')
    def execute(self, context):
        try:
            result = operations.workflow(context)
            self.report({'WARNING'} if result['failures'] else {'INFO'}, f'Output verified: {result["score"]}/100; {len(result["failures"])} skipped/failed operations')
            return {'FINISHED'}
        except Exception as exc:
            traceback.print_exc()
            self.report({'ERROR'}, 'Preparation stopped; inspect output and Undo if needed. ' + str(exc))
            return {'CANCELLED'}

class GR_OT_action(bpy.types.Operator):
    bl_idname = 'gr.action'
    bl_label = 'GameReady Tool'
    bl_options = {'REGISTER','UNDO'}
    action: StringProperty()
    def invoke(self, context, event):
        if self.action in {'LOD','COLLIDER','PIVOT','DENSITY','FIX','RESIZE','PACK','RELINK','PURGE','RENAME'}:
            return context.window_manager.invoke_props_dialog(self, width=450)
        return self.execute(context)
    def draw(self, context):
        descriptions = {'LOD':'Create independent evaluated LOD copies; source preserved.', 'COLLIDER':'Create collision helper meshes; source preserved.', 'PIVOT':'Move origins on eligible meshes. World geometry remains in place.', 'DENSITY':'Scale active UVs around (0.5, 0.5); tiles/overlaps may change.', 'FIX':'Run this individual fix on preserved copies when enabled.', 'RESIZE':'Save a resized/converted image copy to a new file; materials unchanged.', 'PACK':'Save a new RGBA data image using each source red channel.', 'RELINK':'Change this image datablock to the specified file.', 'PURGE':'Remove only the explicitly selected image if it has zero users.', 'RENAME':'Rename selected objects and their mesh datablocks.'}
        self.layout.label(text=descriptions.get(self.action, self.action))
        if self.action=='FIX':
            rows=context.scene.gr_issues
            if rows and context.scene.gr_issue_index<len(rows):
                self.layout.label(text=rows[context.scene.gr_issue_index].message)
    def execute(self, context):
        s=context.scene.gr
        try:
            objects = core.scope(context)
            if self.action == 'SELECT':
                rows=context.scene.gr_issues
                if not rows: raise ValueError('Scan first.')
                obj=bpy.data.objects.get(rows[context.scene.gr_issue_index].object)
                if not obj: raise ValueError('This finding is scope-wide or the object no longer exists.')
                for selected in context.selected_objects: selected.select_set(False)
                obj.hide_set(False)
                obj.select_set(True)
                context.view_layer.objects.active=obj
            elif self.action=='FIX':
                if not context.scene.gr_issues: raise ValueError('Scan first.')
                issue=context.scene.gr_issues[context.scene.gr_issue_index]
                obj=bpy.data.objects.get(issue.object)
                action=issue.fix
                if not obj or not action: raise ValueError('This finding requires manual review.')
                if s.preserve: obj=core.duplicate([obj], context.scene)[0]
                operations.fix(obj, action, s)
                for old in context.selected_objects: old.select_set(False)
                obj.select_set(True)
                context.view_layer.objects.active=obj
                s.scope='SELECTED'
                analysis.refresh(context, [obj])
                core.log(context.scene, f'{action} executed on {obj.name}; review fresh findings for verification.')
            elif self.action in {'LOD','COLLIDER'}:
                require_objects(context)
                count=0
                failures=[]
                for obj in objects:
                    if obj.type!='MESH': continue
                    try:
                        count += len(operations.lods(obj,s) if self.action=='LOD' else operations.collider(obj,s))
                    except Exception as exc:
                        failures.append(str(exc))
                core.log(context.scene, f'{self.action}: {count} helpers created. ' + '; '.join(failures))
                self.report({'WARNING'} if failures else {'INFO'}, f'{count} helpers created; {len(failures)} skipped. Inspect silhouette and collision fit.')
            elif self.action=='PREVIEW_LOD':
                for helper in context.scene.objects:
                    if helper.get('gr_helper')=='LOD':
                        source=bpy.data.objects.get(helper.get('gr_lod_source',''))
                        if source:
                            source.hide_set(s.lod_preview>=0)
                            helper.hide_set(helper.get('gr_lod_level')!=s.lod_preview)
            elif self.action in {'PIVOT','PIVOT_PREVIEW'}:
                require_objects(context)
                for obj in objects:
                    if obj.type!='MESH': continue
                    point=operations.pivot_position(obj,s.pivot_mode,context)
                    if self.action=='PIVOT_PREVIEW': context.scene.cursor.location=point
                    else:
                        if s.preserve: obj=core.duplicate([obj],context.scene)[0]
                        operations.set_pivot(obj,point)
            elif self.action=='DENSITY':
                data=analysis.scan(require_objects(context),s)
                for obj in objects:
                    current=data['density'].get(obj.name,0)
                    if not current: continue
                    if s.preserve: obj=core.duplicate([obj],context.scene)[0]
                    textures.set_density(obj,current,s.density_target)
            elif self.action=='RENAME':
                for obj in require_objects(context): operations.fix(obj,'NAME',s)
            elif self.action=='RESIZE':
                image=bpy.data.images.get(s.image_name)
                if not image: raise ValueError('Choose an image first.')
                clone=textures.copy_image(image,bpy.path.abspath(s.output_dir),s.max_texture,s.image_format)
                core.log(context.scene, f'Saved image copy {clone.name}; original materials still reference original image.')
            elif self.action=='PACK': textures.channel_pack(s)
            elif self.action=='PACK_IMAGE':
                image=bpy.data.images.get(s.image_name)
                if not image: raise ValueError('Choose an image first.')
                image.pack()
            elif self.action=='RELINK':
                image=bpy.data.images.get(s.image_name)
                path=Path(bpy.path.abspath(s.relink_path))
                if not image or not path.is_file(): raise ValueError('Choose an image and an existing replacement file.')
                image.filepath=str(path)
                image.reload()
            elif self.action=='PURGE':
                image=bpy.data.images.get(s.image_name)
                if not image or image.users: raise ValueError('Only a selected zero-user image can be removed.')
                bpy.data.images.remove(image)
            elif self.action=='PROFILE':
                budgets={'MOBILE':(10000,1024,32,2),'PC':(50000,4096,256,8),'VR':(20000,2048,128,4),'WEB':(8000,1024,32,2),'UNITY_MOBILE':(10000,1024,32,2),'UNITY_PC':(50000,4096,256,8),'UNREAL_PROP':(80000,4096,256,8),'AI_RESCUE':(30000,2048,128,4)}
                if s.profile in budgets:
                    s.triangle_budget,s.max_texture,s.texture_budget_mb,s.max_materials=budgets[s.profile]
                if s.profile.startswith('UNITY'): s.engine='UNITY'
                elif s.profile=='UNREAL_PROP': s.engine='UNREAL'
                if s.profile=='AI_RESCUE':
                    s.preserve=True
                    s.level='BALANCED'
                    s.optimize=True
                    s.make_lods=True
                    s.make_colliders=True
                self.engine_preset(s)
            elif self.action=='ENGINE': self.engine_preset(s)
            elif self.action=='SAVE_PROFILE':
                profile=next((p for p in context.scene.gr_profiles if p.name==s.profile_name),None)
                if profile is None: profile=context.scene.gr_profiles.add()
                profile.name=s.profile_name
                excluded={'rna_type','report_json','stale','tab','profile_name'}
                profile.data=json.dumps({p.identifier:getattr(s,p.identifier) for p in s.bl_rna.properties if p.identifier not in excluded})
            elif self.action=='LOAD_PROFILE':
                profile=next((p for p in context.scene.gr_profiles if p.name==s.profile_name),None)
                if not profile: raise ValueError('No saved profile with that name in this blend file.')
                for key,value in json.loads(profile.data).items():
                    if hasattr(s,key): setattr(s,key,value)
            elif self.action=='EXPORT':
                if s.axis_forward[-1]==s.axis_up[-1] and s.export_format!='GLB': raise ValueError('Forward and up axes must differ.')
                path=exporting.export_asset(context)
                self.report({'INFO'}, 'Exported ' + path.name)
            elif self.action=='REPORT':
                self.report({'INFO'}, 'Saved ' + exporting.write_report(context).name)
            elif self.action=='COPY_REPORT': context.window_manager.clipboard=s.report_json
            else: raise ValueError('Unknown action: ' + self.action)
            if self.action not in {'SELECT','FIX','EXPORT','REPORT','COPY_REPORT','SAVE_PROFILE'}: s.stale=True
            return {'FINISHED'}
        except Exception as exc:
            traceback.print_exc()
            core.log(context.scene, f'{self.action} failed: {exc}')
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
    @staticmethod
    def engine_preset(s):
        s.axis_forward='-Y' if s.engine=='UNREAL' else '-Z'
        s.axis_up='Z' if s.engine=='UNREAL' else 'Y'
        s.export_scale=1
        s.prefix='SM_' if s.engine in {'UNREAL','GENERIC'} else ''

class GR_UL_issues(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row=layout.row()
        row.label(text=item.category, icon={'ERROR':'CANCEL','WARNING':'ERROR','INFO':'INFO'}.get(item.severity,'INFO'))
        row.label(text=item.message)

def button(layout, label, action, icon='NONE'):
    op=layout.operator('gr.action',text=label,icon=icon)
    op.action=action
    return op

def wrap(layout, text, width=48):
    import textwrap
    if bpy.context.region:
        width = max(24,min(width,int(bpy.context.region.width/8)-5))
    for line in textwrap.wrap(text,width): layout.label(text=line)

class GR_PT_main(bpy.types.Panel):
    bl_label='GameReady One-Click'
    bl_idname='GR_PT_main'
    bl_space_type='VIEW_3D'
    bl_region_type='UI'
    bl_category='GameReady'
    def draw(self,context):
        l=self.layout
        s=context.scene.gr
        l.label(text='FROM BLENDER TO ENGINE. READY.',icon='CHECKMARK')
        l.prop(s,'tab',text='')
        l.prop(s,'scope')
        data=core.report(context.scene)
        tab=s.tab
        if tab=='DASHBOARD':
            l.label(text='Asset: ' + (context.object.name if context.object else 'No active object'),icon='OBJECT_DATA')
            box=l.box()
            box.label(text=f'GAME READINESS  {data.get("score", "—")}/100',icon='CHECKMARK')
            if s.stale: box.label(text='Scan required / results may be stale',icon='FILE_REFRESH')
            for category,score in data.get('categories',{}).items():
                row=box.row()
                row.label(text=category)
                row.label(text=str(score),icon='CHECKMARK' if score==100 else 'ERROR')
            l.operator('gr.scan',icon='VIEWZOOM')
            row=l.row(); row.scale_y=1.6; row.operator('gr.prepare',icon='MODIFIER')
            l.prop(s,'preserve')
            l.prop(s,'profile')
            button(l,'Apply Production Preset','PROFILE','PRESET')
            wrap(l,'Blender-side checks. Validate the exported asset in your engine.')
        elif tab=='SCAN':
            l.operator('gr.scan',icon='VIEWZOOM')
            l.template_list('GR_UL_issues','',context.scene,'gr_issues',context.scene,'gr_issue_index',rows=7)
            rows=context.scene.gr_issues
            if rows and 0<=context.scene.gr_issue_index<len(rows):
                issue=rows[context.scene.gr_issue_index]
                box=l.box()
                box.label(text=issue.severity+' | '+(issue.object or 'Scope-wide'))
                wrap(box,issue.message)
                wrap(box,issue.recommendation)
                button(box,'Select Affected Object','SELECT','RESTRICT_SELECT_OFF')
                if issue.fix: button(box,'Review Individual Fix','FIX','TOOL_SETTINGS')
            l.label(text='INFO findings do not reduce the score.')
        elif tab=='FIX':
            l.prop(s,'level',expand=True)
            for prop in ('preserve','original_action','fix_names','fix_transforms','merge_distance'): l.prop(s,prop)
            l.operator('gr.prepare',icon='MODIFIER')
            box=l.box(); box.label(text='Naming')
            for prop in ('prefix','suffix','rename_pattern','rename_replace'): box.prop(s,prop)
            button(box,'Rename Scope','RENAME')
            box=l.box(); box.label(text='Origins / Pivots')
            box.prop(s,'pivot_mode')
            button(box,'Preview at 3D Cursor','PIVOT_PREVIEW','PIVOT_CURSOR')
            button(box,'Set Pivot','PIVOT')
        elif tab=='OPTIMIZE':
            for prop in ('profile','triangle_budget','texture_budget_mb','max_materials','max_texture','optimize','make_lods','make_colliders'): l.prop(s,prop)
            button(l,'Apply Preset','PROFILE')
            l.operator('gr.prepare',icon='MOD_DECIM')
            box=l.box(); box.label(text='Measured statistics (evaluated meshes)')
            for key,value in data.get('statistics',{}).items():
                before=data.get('before',{}).get(key)
                if key=='texture_bytes':
                    box.label(text='Texture MiB: '+(f'{before/1024**2:.1f} → ' if before is not None else '')+f'{value/1024**2:.1f}')
                else:
                    box.label(text=f'{key.replace("_"," ").title()}: '+(f'{before:,} → ' if before is not None else '')+f'{value:,}')
            box=l.box(); box.label(text='Texel density')
            box.prop(s,'density_resolution'); box.prop(s,'density_target')
            for name,value in data.get('density',{}).items(): box.label(text=f'{name}: {value:.1f} px/unit')
            button(box,'Set Density on Preserved Copies','DENSITY')
        elif tab=='LOD':
            l.prop(s,'lod_count')
            for i in range(1,s.lod_count+1): l.prop(s,'lod'+str(i))
            button(l,'Generate LOD0 + Reduced Levels','LOD','MOD_DECIM')
            l.prop(s,'lod_preview'); button(l,'Preview LOD / Restore Source','PREVIEW_LOD')
            wrap(l,'Decimation is approximate. Inspect silhouettes, UVs and shading. Rigged and shape-key meshes are skipped.')
            for obj in context.scene.objects:
                if obj.get('gr_helper')=='LOD': l.label(text=obj.name)
        elif tab=='COLLIDERS':
            l.prop(s,'collider_mode'); l.prop(s,'engine')
            button(l,'Generate Collision Helpers','COLLIDER','MESH_ICOSPHERE')
            wrap(l,'Smart uses dimension ratios: near-equal → sphere; tall → capsule; otherwise box. Compound creates a hull per connected component, not convex decomposition.')
            wrap(l,'Helpers require collision setup in the target engine. Mesh collision can be expensive.')
        elif tab=='TEXTURES':
            l.operator('gr.scan',icon='VIEWZOOM')
            for image in data.get('textures',[]):
                box=l.box(); box.label(text=image['name'],icon='IMAGE_DATA')
                box.label(text=f'{image["width"]} × {image["height"]} | {image["bytes"]/1024**2:.2f} MiB estimated')
                box.label(text=f'{image["kind"]} | {image["colorspace"]} | {image["depth"]} bit')
                if image['warning']: wrap(box,image['warning'])
            l.prop_search(s,'image_name',bpy.data,'images')
            l.prop(s,'max_texture'); l.prop(s,'image_format'); l.prop(s,'output_dir')
            button(l,'Save Resized / Converted Copy','RESIZE')
            button(l,'Pack Selected Image into Blend','PACK_IMAGE')
            l.prop(s,'relink_path'); button(l,'Relink Selected Image','RELINK')
            button(l,'Remove Selected Zero-user Image','PURGE')
            box=l.box(); box.label(text='Channel packing (source red channels)')
            for prop in ('pack_r','pack_g','pack_b','pack_a'): box.prop_search(s,prop,bpy.data,'images')
            box.prop(s,'pack_resolution'); button(box,'Save Packed Texture','PACK')
        elif tab=='EXPORT':
            l.prop(s,'engine'); button(l,'Load Engine Axes','ENGINE')
            l.label(text='Blender-side preparation only',icon='INFO')
            for prop in ('export_format','output_dir','export_name','export_scale'): l.prop(s,prop)
            if s.export_format!='GLB': l.prop(s,'axis_forward'); l.prop(s,'axis_up')
            for prop in ('export_animation','export_armatures','triangulate','export_lods','export_colliders'): l.prop(s,prop)
            if s.export_format!='FBX': l.prop(s,'export_materials')
            else: l.label(text='FBX exports assigned materials.')
            if s.export_format=='FBX': l.prop(s,'embed_textures')
            l.operator('gr.scan',text='Validate Before Export',icon='CHECKMARK')
            l.prop(s,'override_errors')
            button(l,'Validate & Export','EXPORT','EXPORT')
        elif tab=='REPORT':
            l.prop(s,'output_dir')
            button(l,'Save TXT + JSON + HTML','REPORT','FILE_TEXT')
            button(l,'Copy JSON to Clipboard','COPY_REPORT','COPYDOWN')
            l.prop(s,'profile_name')
            row=l.row(); button(row,'Save Profile','SAVE_PROFILE'); button(row,'Load Profile','LOAD_PROFILE')
            l.label(text='Profiles are stored in this blend file.')
            for entry in list(context.scene.gr_history)[-15:]: wrap(l,entry.message)

class GRPreferences(bpy.types.AddonPreferences):
    bl_idname=__package__
    def draw(self,context):
        self.layout.label(text='GameReady One-Click 1.0.0 | Tested with Blender 5.1.2')
        self.layout.label(text='Open the 3D View sidebar (N) > GameReady.')
        self.layout.label(text='Preserve Original is enabled by default. See included documentation.')

@persistent
def invalidate(scene, depsgraph):
    if hasattr(scene,'gr') and any(update.is_updated_geometry or update.is_updated_transform or isinstance(update.id,(bpy.types.Image,bpy.types.Material,bpy.types.Mesh)) for update in depsgraph.updates):
        scene.gr.stale=True

CLASSES=(GRSettings,GRIssue,GRHistory,GRProfile,GR_OT_scan,GR_OT_prepare,GR_OT_action,GR_UL_issues,GR_PT_main,GRPreferences)

def register():
    for cls in CLASSES: bpy.utils.register_class(cls)
    bpy.types.Scene.gr=PointerProperty(type=GRSettings)
    bpy.types.Scene.gr_issues=CollectionProperty(type=GRIssue)
    bpy.types.Scene.gr_history=CollectionProperty(type=GRHistory)
    bpy.types.Scene.gr_profiles=CollectionProperty(type=GRProfile)
    bpy.types.Scene.gr_issue_index=IntProperty(default=0,min=0)
    if invalidate not in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.append(invalidate)

def unregister():
    if invalidate in bpy.app.handlers.depsgraph_update_post: bpy.app.handlers.depsgraph_update_post.remove(invalidate)
    for name in ('gr','gr_issues','gr_history','gr_profiles','gr_issue_index'): delattr(bpy.types.Scene,name)
    for cls in reversed(CLASSES): bpy.utils.unregister_class(cls)
