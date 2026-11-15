# MacroPad Mixer - boot.py
# Runs only on power-up / hard reset (plugging in, or the RESET button).
#
# - Enables the second USB serial port ("data") that the host daemon uses; the first one stays
#   the normal REPL console.
# - Hides the CIRCUITPY drive in normal use, so unplugging never risks corrupting it and macOS
#   doesn't warn "Disk Not Ejected Properly". Hold KEY 1 (top-left key, right under the screen)
#   while plugging in to show the drive, e.g. to run the installer or update the firmware.
#   (Safe mode also always shows the drive: press RESET, then press it again while the LED
#   blinks yellow.)
import board
import digitalio
import storage
import usb_cdc

usb_cdc.enable(console=True, data=True)

key1 = digitalio.DigitalInOut(board.KEY1)
key1.switch_to_input(pull=digitalio.Pull.UP)
if key1.value:  # not pressed (keys pull the pin low)
    storage.disable_usb_drive()
key1.deinit()
