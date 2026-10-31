"""GameReady One-Click: transparent Blender-side asset preparation."""
bl_info = {"name": "GameReady One-Click", "author": "GameReady Project", "version": (1, 0, 0), "blender": (5, 1, 0), "location": "View3D > Sidebar > GameReady", "description": "Scan, review, prepare and export game assets with preserved originals", "category": "Object"}

def register():
    from . import ui
    ui.register()

def unregister():
    from . import ui
    ui.unregister()
