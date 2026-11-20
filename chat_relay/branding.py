"""Draw the Chat Relay notification icon at native Windows sizes."""
from PIL import Image, ImageDraw


def image():
    icon = Image.new('RGBA', (128, 128))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((3, 3, 124, 124), radius=28, fill='#112940', outline='#4486AC', width=5)
    draw.rounded_rectangle((22, 25, 71, 56), radius=10, fill='#EAF8FF')
    draw.polygon(((31, 51), (31, 66), (48, 52)), fill='#EAF8FF')
    draw.rounded_rectangle((57, 70, 106, 101), radius=10, fill='#83DCFF')
    draw.polygon(((80, 73), (97, 61), (97, 76)), fill='#83DCFF')
    green = '#74E8AB'
    draw.line((80, 40, 103, 40), fill=green, width=7)
    draw.line((94, 30, 104, 40, 94, 50), fill=green, width=7, joint='curve')
    draw.line((48, 86, 25, 86), fill=green, width=7)
    draw.line((34, 76, 24, 86, 34, 96), fill=green, width=7, joint='curve')
    return icon
