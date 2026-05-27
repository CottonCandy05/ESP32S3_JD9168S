"""
================================================================================
test.py
Hardware showcase / validation script for the 5.7" JD9168S display on
ESP32-S3 running custom lvgl_micropython firmware.

Sequence:
    1. Three big "Hello World" labels (Red-Center, Green-Left, Blue-Right)
    2. 5-second countdown overlay
    3. Full-screen colour sweep: Red -> Green -> Blue (2 s each)
    4. Touch test: tap the screen, a dot is drawn wherever you touched;
       a "Clear" button wipes the canvas. Runs forever.

IMPORTANT: After this script has been run once, DO NOT use Thonny's
"Soft Reboot" or "Stop/Restart" button to re-run it. The ESP32-S3 RGB
peripheral keeps running after a soft reboot, and re-initialising it
will panic the CPU and drop the USB connection. Unplug and replug the
board to re-run the test.
================================================================================
"""

import time
import lvgl as lv
import task_handler
import os
from JD9168S_driver import JD9168S_Display

_log = open('test_log.txt', 'w')
os.dupterm(_log)

# ------------------------------------------------------------------------- #
# 1. Bring up the hardware                                                   #
# ------------------------------------------------------------------------- #
display = JD9168S_Display(width=640, height=480, backlight_pin=2)
display.debug_memory()

# Start LVGL's periodic task handler (ticks + refresh on a timer)
th = task_handler.TaskHandler()

scrn = lv.screen_active()
scrn.set_style_bg_color(lv.color_hex(0x000000), 0)
scrn.set_style_bg_opa(lv.OPA.COVER, 0)

WIDTH, HEIGHT = 640, 480


def refresh_ms(ms):
    """Let LVGL tick/draw while we sleep."""
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < ms:
        lv.timer_handler()
        time.sleep_ms(5)


def big_label(parent, text, color_hex, x_align, y_offset=0):
    """Create one of the big Hello World labels."""
    lbl = lv.label(parent)
    lbl.set_text(text)
    lbl.set_style_text_color(lv.color_hex(color_hex), 0)
    # Font size: scale the built-in montserrat_28 via transform for a big feel.
    # (Built-in large fonts depend on lv_conf flags; montserrat_28 is safe.)
    try:
        lbl.set_style_text_font(lv.font_montserrat_28, 0)
    except AttributeError:
        pass
    lbl.set_style_transform_scale(512, 0)  # 2x scale (256 = 1x)
    lbl.align(x_align, 0, y_offset)


# ------------------------------------------------------------------------- #
# 2. Three Hello World labels                                                #
# ------------------------------------------------------------------------- #
print("[TEST] Showing RGB Hello World labels...")

big_label(scrn, "Hello World", 0x00FF00, lv.ALIGN.LEFT_MID, 0)     # Green
big_label(scrn, "Hello World", 0xFF0000, lv.ALIGN.CENTER,  0)      # Red
big_label(scrn, "Hello World", 0x0000FF, lv.ALIGN.RIGHT_MID, 0)    # Blue

# Force a first draw before the countdown starts
refresh_ms(200)


# ------------------------------------------------------------------------- #
# 3. 5-second countdown overlay                                              #
# ------------------------------------------------------------------------- #
print("[TEST] Countdown...")

countdown_lbl = lv.label(scrn)
countdown_lbl.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
try:
    countdown_lbl.set_style_text_font(lv.font_montserrat_28, 0)
except AttributeError:
    pass
countdown_lbl.set_style_transform_scale(768, 0)  # 3x scale
countdown_lbl.align(lv.ALIGN.BOTTOM_MID, 0, -30)

for n in range(5, 0, -1):
    countdown_lbl.set_text("Color test in {}...".format(n))
    refresh_ms(1000)

countdown_lbl.delete()
refresh_ms(50)


# ------------------------------------------------------------------------- #
# 4. Full-screen colour sweep                                                #
# ------------------------------------------------------------------------- #
print("[TEST] Colour sweep...")

# Wipe previous children so the background fills the whole display
scrn.clean()

colour_panel = lv.obj(scrn)
colour_panel.set_size(WIDTH, HEIGHT)
colour_panel.align(lv.ALIGN.CENTER, 0, 0)
colour_panel.set_style_border_width(0, 0)
colour_panel.set_style_pad_all(0, 0)
colour_panel.set_style_radius(0, 0)

for name, rgb in (("RED", 0xFF0000), ("GREEN", 0x00FF00), ("BLUE", 0x0000FF)):
    print("[TEST]   -> {}".format(name))
    colour_panel.set_style_bg_color(lv.color_hex(rgb), 0)
    colour_panel.set_style_bg_opa(lv.OPA.COVER, 0)
    refresh_ms(2000)


# ------------------------------------------------------------------------- #
# 5. Touch test                                                              #
# ------------------------------------------------------------------------- #
print("[TEST] Entering touch test. Tap the screen to draw dots.")

scrn.clean()
scrn.set_style_bg_color(lv.color_hex(0x101820), 0)

title = lv.label(scrn)
title.set_text("Touch Test - tap anywhere")
title.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
try:
    title.set_style_text_font(lv.font_montserrat_28, 0)
except AttributeError:
    pass
title.align(lv.ALIGN.TOP_MID, 0, 15)

# A full-screen canvas-like object we draw dots onto
canvas = lv.obj(scrn)
canvas.set_size(WIDTH, HEIGHT - 80)
canvas.align(lv.ALIGN.TOP_MID, 0, 60)
canvas.set_style_bg_color(lv.color_hex(0x000000), 0)
canvas.set_style_border_color(lv.color_hex(0x404040), 0)
canvas.set_style_border_width(2, 0)
canvas.set_style_radius(0, 0)
canvas.set_style_pad_all(0, 0)
canvas.remove_flag(lv.obj.FLAG.SCROLLABLE)

# A "Clear" button in the bottom-right
clear_btn = lv.button(scrn)
clear_btn.set_size(140, 60)
clear_btn.align(lv.ALIGN.BOTTOM_RIGHT, -15, -10)
clear_btn.set_style_bg_color(lv.color_hex(0xCC3333), 0)
clear_lbl = lv.label(clear_btn)
clear_lbl.set_text("CLEAR")
clear_lbl.set_style_text_color(lv.color_hex(0xFFFFFF), 0)
clear_lbl.center()


def _on_clear(evt):
    # Delete every dot child but keep the canvas itself
    for i in range(canvas.get_child_count() - 1, -1, -1):
        canvas.get_child(i).delete()

clear_btn.add_event_cb(_on_clear, lv.EVENT.CLICKED, None)


def _draw_dot_at(x_screen, y_screen):
    # Convert screen coords to coords relative to the canvas
    cx = canvas.get_x()
    cy = canvas.get_y()
    cw = canvas.get_width()
    ch = canvas.get_height()
    lx = x_screen - cx
    ly = y_screen - cy
    if lx < 0 or ly < 0 or lx >= cw or ly >= ch:
        return
    dot = lv.obj(canvas)
    dot.set_size(14, 14)
    dot.set_pos(lx - 7, ly - 7)
    dot.set_style_radius(lv.RADIUS.CIRCLE, 0)
    dot.set_style_bg_color(lv.color_hex(0x33FF88), 0)
    dot.set_style_border_width(0, 0)
    dot.remove_flag(lv.obj.FLAG.CLICKABLE)
    dot.remove_flag(lv.obj.FLAG.SCROLLABLE)


def _on_screen_press(evt):
    indev = lv.indev_active()
    if indev is None:
        return
    pt = lv.point_t()
    indev.get_point(pt)
    # Ignore presses that land on the clear button
    tgt = evt.get_target_obj()
    if tgt == clear_btn or (tgt is not None and tgt.get_parent() == clear_btn):
        return
    _draw_dot_at(pt.x, pt.y)


scrn.add_event_cb(_on_screen_press, lv.EVENT.PRESSED, None)

print("[TEST] Touch test running. Press the red CLEAR button to wipe dots.")

# Hand control to LVGL forever
while True:
    lv.timer_handler()
    time.sleep_ms(5)