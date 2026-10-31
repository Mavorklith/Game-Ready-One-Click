"""Texture writes always use new paths and duplicated datablocks."""
import bpy
import math
from array import array
from pathlib import Path
from .core import unique_path, safe_name

def copy_image(image, folder, resolution, fmt='PNG'):
    if image.source not in {'FILE','GENERATED'} or not image.size[0] or not image.size[1]:
        raise ValueError('Only loaded single images are supported; UDIM, movie and sequence workflows need manual handling.')
    clone = image.copy()
    clone.name = image.name + '_GameReady'
    ratio = min(1, resolution/max(image.size))
    if ratio < 1:
        clone.scale(max(1,round(image.size[0]*ratio)), max(1,round(image.size[1]*ratio)))
    clone.file_format = fmt
    clone.filepath_raw = str(unique_path(Path(folder) / (safe_name(image.name) + ('.png' if fmt=='PNG' else '.exr'))))
    clone.save()
    return clone

def channel_pack(settings):
    size = settings.pack_resolution
    if size > 2048:
        raise ValueError('Channel packing is limited to 2048 to bound memory and interactive stalls.')
    result = array('f', [0]) * (size*size*4)
    for channel, name in enumerate((settings.pack_r, settings.pack_g, settings.pack_b, settings.pack_a)):
        image = bpy.data.images.get(name)
        if name and not image:
            raise ValueError(f'Channel source not found: {name}')
        if image:
            if image.source not in {'FILE','GENERATED'} or not image.has_data:
                raise ValueError(f'{name} must be a loaded single image.')
            clone = image.copy()
            try:
                clone.scale(size,size)
                pixels = array('f', [0]) * (size*size*4)
                clone.pixels.foreach_get(pixels)
                result[channel::4] = pixels[0::4]
            finally:
                bpy.data.images.remove(clone)
        else:
            result[channel::4] = array('f', [1 if channel==3 else 0]) * (size*size)
    image = bpy.data.images.new('GameReady_ORM', width=size, height=size, alpha=True)
    image.colorspace_settings.name = 'Non-Color'
    image.pixels.foreach_set(result)
    image.file_format = 'PNG'
    image.filepath_raw = str(unique_path(Path(bpy.path.abspath(settings.output_dir)) / 'GameReady_ORM.png'))
    image.save()
    return image

def set_density(obj, current, target):
    from .core import static_guard
    static_guard(obj)
    if current <= 0 or not obj.data.uv_layers.active:
        raise ValueError('A non-empty UV map and a positive measured density are required.')
    if obj.data.users > 1: obj.data = obj.data.copy()
    data = obj.data.uv_layers.active.data
    factor = target/current
    for loop in data:
        loop.uv = (loop.uv - __import__('mathutils').Vector((.5,.5))) * factor + __import__('mathutils').Vector((.5,.5))
