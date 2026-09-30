import time
import board
import neopixel_write
import digitalio
import random

import displayio
import busio
import terminalio
from adafruit_display_text import label
import adafruit_displayio_ssd1306
import i2cdisplaybus

import pwmio
import microcontroller
import struct

# =========================================
#  Einfacher NeoPixel-Treiber (WS2812/WS2812E)
# =========================================

class SimpleNeoPixel:
    def __init__(self, pin, n, brightness=1.0, auto_write=True):
        self.pin = digitalio.DigitalInOut(pin)
        self.pin.direction = digitalio.Direction.OUTPUT

        self.n = n
        self.auto_write = auto_write
        self.brightness = brightness
        self._buf = bytearray(n * 3)  # GRB

    def _encode_color(self, color):
        r, g, b = color
        r = int(r * self.brightness)
        g = int(g * self.brightness)
        b = int(b * self.brightness)
        # WS2812 = GRB
        return g, r, b

    def __setitem__(self, index, color):
        if isinstance(index, slice):
            start, stop, step = index.indices(self.n)
            for i in range(start, stop, step):
                self._set_pixel(i, color)
        else:
            self._set_pixel(index, color)

        if self.auto_write:
            self.show()

    def _set_pixel(self, i, color):
        if not (0 <= i < self.n):
            return
        g, r, b = self._encode_color(color)
        base = i * 3
        self._buf[base] = g
        self._buf[base + 1] = r
        self._buf[base + 2] = b

    def fill(self, color):
        g, r, b = self._encode_color(color)
        for i in range(self.n):
            base = i * 3
            self._buf[base] = g
            self._buf[base + 1] = r
            self._buf[base + 2] = b
        if self.auto_write:
            self.show()

    def show(self):
        neopixel_write.neopixel_write(self.pin, self._buf)


# =========================================
#            SPIELKONFIGURATION
# =========================================

NUM_LEDS = 60
if hasattr(board, "IO0"):
    # Maker Go ESP32-C3 Supermini
    PIXEL_PIN = board.IO7
    BTN_RED_PIN = board.IO0
    BTN_GREEN_PIN = board.IO1
    BTN_BLUE_PIN = board.IO3
    OLED_SCL_PIN = board.IO4
    OLED_SDA_PIN = board.IO5
    AUDIO_PIN = board.IO6
else:
    # RP2040 Zero
    PIXEL_PIN = board.GP0
    BTN_RED_PIN = board.GP10
    BTN_GREEN_PIN = board.GP11
    BTN_BLUE_PIN = board.GP12
    OLED_SCL_PIN = board.GP5
    OLED_SDA_PIN = board.GP4
    AUDIO_PIN = board.GP15

BRIGHTNESS = 0.4

# Farben (R, G, B)
COLORS = [
    (255, 0, 0),   # 0 = Rot
    (0, 255, 0),   # 1 = Grün
    (0, 0, 255),   # 2 = Blau
]

# =========================================
#           HARDWARE INITIALISIEREN
# =========================================

pixels = SimpleNeoPixel(PIXEL_PIN, NUM_LEDS, brightness=BRIGHTNESS, auto_write=False)

btn_red = digitalio.DigitalInOut(BTN_RED_PIN)
btn_red.switch_to_input(pull=digitalio.Pull.UP)
btn_green = digitalio.DigitalInOut(BTN_GREEN_PIN)
btn_green.switch_to_input(pull=digitalio.Pull.UP)
btn_blue = digitalio.DigitalInOut(BTN_BLUE_PIN)
btn_blue.switch_to_input(pull=digitalio.Pull.UP)

buttons = [btn_red, btn_green, btn_blue]
prev_button_state = [True, True, True]

# ====== OLED INITIALISIEREN (SSD1306 128x64, CP 10-kompatibel) ======
displayio.release_displays()
i2c = busio.I2C(OLED_SCL_PIN, OLED_SDA_PIN)

display_bus = i2cdisplaybus.I2CDisplayBus(i2c, device_address=0x3C)

OLED_WIDTH = 128
OLED_HEIGHT = 64

oled = adafruit_displayio_ssd1306.SSD1306(
    display_bus, width=OLED_WIDTH, height=OLED_HEIGHT
)

oled_root = displayio.Group()
oled.root_group = oled_root

# HUD- und Effekt-Labels einmalig anlegen
hud_label = label.Label(terminalio.FONT, text="", x=0, y=10)
effect_label = label.Label(terminalio.FONT, text="", x=0, y=32)  # mittig

oled_root.append(hud_label)
oled_root.append(effect_label)

# ====== AUDIO INITIALISIEREN (PAM8302) ======
audio = pwmio.PWMOut(
    AUDIO_PIN, frequency=440, duty_cycle=0, variable_frequency=True
)

# =========================================
#              SPIELZUSTAND
# =========================================

strip_state = [None] * NUM_LEDS    # fallende Blöcke
shots = []                         # (color_index, position)

score = 0
last_score = 0
highscore = 0
level = 1
lives = 3

# Waffen / Powerups
weapon_slow_until = 0.0        # „Slow Time“
weapon_mono_until = 0.0        # „Mono Color“
weapon_mono_color = None
weapon_rocket_charges = 0      # Anzahl „Durchschuss“-Raketen

# Level-Fortschritt basiert auf zerstörten Blöcken, nicht auf Score
blocks_cleared_total = 0
speedup_every = 10          # alle 10 zerstörten Blöcke ein Level-Up
next_level_blocks = speedup_every

game_state = "START"   # "START", "RUNNING", "GAME_OVER"

step_time = 1.5
min_step_time = 0.05
last_step = time.monotonic()

shot_step_time = 0.01   # Schussgeschwindigkeit
last_shot_step = time.monotonic()

# Effekttexte
effect_text = None
effect_timer = 0.0

# Sound-State (non-blocking SFX-Engine)
current_sfx = None       # Liste von (freq, duration)
sfx_index = 0
sfx_step_end = 0.0
sfx_playing = False


# =========================================
#           HIGHSCORE PERSISTENZ (NVM)
# =========================================

def load_highscore():
    global highscore
    data = microcontroller.nvm[:4]
    if data == b"\xff\xff\xff\xff":
        highscore = 0
    else:
        try:
            highscore = struct.unpack("I", data)[0]
        except struct.error:
            highscore = 0

def save_highscore():
    data = struct.pack("I", highscore)
    microcontroller.nvm[:4] = data


# =========================================
#           SOUND-EFFEKTE (SID-Style, non-blocking)
# =========================================

def stop_notes():
    audio.duty_cycle = 0

def play_tone(frequency):
    audio.frequency = frequency
    audio.duty_cycle = 32768

def start_sfx(seq):
    """
    seq = Liste von (freq, duration) in Sekunden.
    Startet eine neue SFX-Sequenz, überschreibt ggf. laufende.
    """
    global current_sfx, sfx_index, sfx_step_end, sfx_playing
    current_sfx = seq
    sfx_index = 0
    sfx_step_end = 0.0
    sfx_playing = False  # wird beim ersten update gestartet

def sound_update(now):
    """Muss in der Hauptschleife aufgerufen werden – spielt SFX ab."""
    global current_sfx, sfx_index, sfx_step_end, sfx_playing

    if current_sfx is None:
        return

    # Noch nichts gestartet? -> ersten Ton starten
    if not sfx_playing:
        freq, duration = current_sfx[sfx_index]
        stop_notes()

        if freq > 0:
            play_tone(freq)

        sfx_step_end = now + duration
        sfx_playing = True
        return    

    # Ton läuft – fertig?
    if now >= sfx_step_end:
        stop_notes()
        sfx_index += 1

        # Sequence zu Ende?
        if sfx_index >= len(current_sfx):
            current_sfx = None
            sfx_playing = False
            return

        # nächsten Ton starten
        freq, duration = current_sfx[sfx_index]

        if freq > 0:
            play_tone(freq)

        sfx_step_end = now + duration
        sfx_playing = True


def play_start_new_game():
    # kleines „Arcade-Start“-Jingle, eigen komponiert
    start_sfx([
        (523, 0.08),   # C5
        (659, 0.08),   # E5
        (784, 0.12),   # G5
        (0,   0.04),   # Pause
        (784, 0.08),   # G5
        (988, 0.08),   # B5
        (1046, 0.16),  # C6
    ])

def play_shot():
    # kurzer Doppel-Pew
    start_sfx([
        (1400, 0.03),
        (900,  0.04),
    ])

def play_hit():
    # knackiger Treffer-Ping
    start_sfx([
        (1000, 0.06),
    ])

def play_wrong():
    # tiefer Fehlerton
    start_sfx([
        (250, 0.12),
    ])

def play_level_up():
    # kleines Arpeggio
    start_sfx([
        (500, 0.04),
        (700, 0.04),
        (900, 0.04),
        (1200, 0.04),
    ])

def play_game_over():
    # absteigender „Verloren“-Ton
    start_sfx([
        (600, 0.12),
        (450, 0.12),
        (320, 0.12),
        (220, 0.12),
    ])

def play_weapon_unlock():
    # kurzer „Powerup“-Chime
    start_sfx([
        (800, 0.06),
        (1100, 0.06),
        (1500, 0.10),
    ])

def play_rocket():
    # eigener Effekt für Rakete
    start_sfx([
        (400, 0.04),
        (600, 0.04),
        (900, 0.06),
        (1200, 0.08),
        (0,   0.04),   # Pause
        (1200, 0.08),
        (0,   0.04),   # Pause
        (1200, 0.08),
    ])


# =========================================
#           DISPLAY-HILFSFUNKTIONEN
# =========================================

def update_display():
    global hud_label, effect_label, game_state, score, level, lives, highscore, last_score, effect_text

    # HUD-Text aktualisieren
    if game_state == "START":
        hud_label.text = "Press any button\nto start\n\nHi: {}".format(highscore)

    elif game_state == "RUNNING":
        hud_label.text = "Score: {}\nLevel: {}\nLives: {}\nHi: {}".format(
            score, level, lives, highscore
        )

    elif game_state == "GAME_OVER":
        hud_label.text = "GAME OVER\nScore: {}\nHi: {}\n\nPress any".format(
            last_score, highscore
        )

    # Effekttext (Overlay)
    if effect_text:
        char_width = 6
        text_width = len(effect_text) * char_width
        effect_label.x = max(0, (OLED_WIDTH - text_width) // 2)
        effect_label.y = 32
        effect_label.text = effect_text
    else:
        effect_label.text = ""


def show_effect(msg, duration=1.0):
    """Zeigt einen kurzzeitigen Effekttext über dem HUD."""
    global effect_text, effect_timer
    effect_text = msg
    effect_timer = time.monotonic() + duration
    update_display()


# =========================================
#           SPIEL-HILFSFUNKTIONEN
# =========================================

def unlock_random_weapon():
    global weapon_slow_until, weapon_mono_until, weapon_mono_color, weapon_rocket_charges

    w = random.choice(("slow", "mono", "rocket"))
    now = time.monotonic()

    if w == "slow":
        # z.B. 5 Sekunden Slow Motion
        weapon_slow_until = now + 5.0
        show_effect("SLOW TIME!", 1.0)
        play_weapon_unlock()
        print("Powerup: Slow Time aktiv bis", weapon_slow_until)

    elif w == "mono":
        # 8 Sekunden Monofarben
        weapon_mono_until = now + 8.0
        weapon_mono_color = random.randint(0, 2)

        # Alle existierenden Blöcke einfärben
        for i, val in enumerate(strip_state):
            if val is not None:
                strip_state[i] = weapon_mono_color

        # Bereits abgefeuerte Schüsse anpassen
        for i, (color_idx, pos) in enumerate(shots):
            shots[i] = (weapon_mono_color, pos)

        show_effect("MONO COLOR!", 1.0)
        play_weapon_unlock()
        print("Powerup: Mono Color", weapon_mono_color, "bis", weapon_mono_until)

    elif w == "rocket":
        weapon_rocket_charges += 1
        show_effect("ROCKET +1", 1.0)
        play_weapon_unlock()
        print("Powerup: Rocket Charges:", weapon_rocket_charges)


def spawn_top():
    """Neue Farbe oben spawnen oder Lücke."""
    now = time.monotonic()
    if now < weapon_mono_until and weapon_mono_color is not None:
        # Mono-Color aktiv
        if random.random() < 0.85:
            return weapon_mono_color
        else:
            return None

    # normaler Modus
    if random.random() < 0.85:
        return random.randint(0, 2)
    else:
        return None


def shift_down():
    """Alle Blöcke einen Schritt nach unten schieben."""
    global strip_state
    for i in range(NUM_LEDS - 1):
        strip_state[i] = strip_state[i + 1]
    strip_state[NUM_LEDS - 1] = spawn_top()


def clear_chain_at(index):
    """
    Löscht die zusammenhängende Kette gleicher Farbe um 'index' herum
    und komprimiert nach unten.
    """
    global strip_state
    if index is None:
        return 0
    if strip_state[index] is None:
        return 0

    color = strip_state[index]

    # nach unten ausdehnen
    start = index
    while start > 0 and strip_state[start - 1] == color:
        start -= 1

    # nach oben ausdehnen
    end = index
    while end < NUM_LEDS - 1 and strip_state[end + 1] == color:
        end += 1

    length = end - start + 1

    # alles darüber nach unten schieben
    for i in range(end + 1, NUM_LEDS):
        strip_state[i - length] = strip_state[i]

    # oben auffüllen
    for i in range(NUM_LEDS - length, NUM_LEDS):
        strip_state[i] = None

    return length


def clear_chain_rocket(index):
    """
    Raketen-Clear:
    - löscht die getroffene Kette
    - plus alle Blöcke darüber
    - komprimiert danach das Feld nach unten
    """
    global strip_state
    if index is None:
        return 0
    if strip_state[index] is None:
        return 0

    color = strip_state[index]

    # Kette wie bei clear_chain_at bestimmen
    start = index
    while start > 0 and strip_state[start - 1] == color:
        start -= 1

    end = index
    while end < NUM_LEDS - 1 and strip_state[end + 1] == color:
        end += 1

    # Rakete: alles ab 'start' nach oben wegblasen
    cleared = 0
    for i in range(start, NUM_LEDS):
        if strip_state[i] is not None:
            strip_state[i] = None
            cleared += 1

    # jetzt alles nach unten komprimieren
    write = 0
    for i in range(NUM_LEDS):
        if strip_state[i] is not None:
            strip_state[write] = strip_state[i]
            write += 1
    for i in range(write, NUM_LEDS):
        strip_state[i] = None

    return cleared


def update_shots_and_collisions():
    """
    Schüsse nach oben bewegen, Kollisionen mit Blöcken prüfen.
    - Richtige Farbe: Kette weg + Score + evtl. Speedup.
    - Falsche Farbe: Leben runter + evtl. Game Over.
    """
    global shots, score, lives, step_time, level, game_state
    global blocks_cleared_total, next_level_blocks
    global weapon_rocket_charges

    if game_state != "RUNNING":
        return

    new_shots = []
    for (color_idx, pos) in shots:
        pos += 1  # Schuss nach oben

        if pos >= NUM_LEDS:
            continue  # vom Strip geflogen

        block = strip_state[pos]
        if block is not None:
            # Kollision mit Block
            if block == color_idx:
                # Treffer
                # Prüfen, ob Rakete verfügbar
                if weapon_rocket_charges > 0:
                    cleared = clear_chain_rocket(pos)
                    weapon_rocket_charges -= 1
                    show_effect("ROCKET!", 0.5)
                    play_rocket()
                    print("Rakete gezündet! Chargen übrig:", weapon_rocket_charges)
                else:
                    cleared = clear_chain_at(pos)

                # Punkte berechnen (abhängig vom Level)
                points = cleared * level
                score += points

                # Fortschritt für Level (nur zerstörte Blöcke zählen)
                blocks_cleared_total += cleared

                if weapon_rocket_charges == 0:
                    # nur normalen Hit-Sound zeigen, wenn nicht gerade der Rocket-SFX aktiv war
                    show_effect("HIT!", 0.2)
                    play_hit()

                print(
                    "Treffer! Kette:", cleared,
                    "Level:", level,
                    "Punkte +", points,
                    "Score:", score,
                    "Blocks total:", blocks_cleared_total
                )

                # Waffen-Freischaltung:
                # - ab 4er-Kette
                # - oder wenn das Feld komplett leer ist (Perfect Clear)
                if cleared >= 3 or all(v is None for v in strip_state):
                    unlock_random_weapon()

                # Levelaufstieg + Speedup, basierend auf zerstörten Blöcken
                if blocks_cleared_total >= next_level_blocks and step_time > min_step_time:
                    level += 1
                    step_time = max(min_step_time, step_time - 0.1)
                    next_level_blocks += speedup_every
                    show_effect("LEVEL UP!", 1.0)
                    play_level_up()
                    print("LEVEL UP! Level:", level, "Neue Step-Time:", step_time,
                          "nächster Level bei Blöcken:", next_level_blocks)
            else:
                # Falsche Farbe
                lives -= 1
                step_time = max(min_step_time, step_time - 0.02)
                show_effect("MISS!", 0.3)
                play_wrong()
                print("Falsche Farbe! Leben:", lives, "Step-Time:", step_time)

                if lives <= 0:
                    game_over("Keine Leben mehr")
                    return

            update_display()
            # Schuss verbraucht (egal ob Treffer oder Fail)
            continue
        else:
            # kein Block, Flug geht weiter
            new_shots.append((color_idx, pos))

    shots = new_shots


def draw_strip():
    """Blöcke + Schüsse auf den LEDs zeichnen."""
    for i, val in enumerate(strip_state):
        if val is None:
            pixels[i] = (0, 0, 0)
        else:
            pixels[i] = COLORS[val]

    for (color_idx, pos) in shots:
        if 0 <= pos < NUM_LEDS:
            pixels[pos] = COLORS[color_idx]

    pixels.show()


def game_over(reason=""):
    global strip_state, score, last_score, lives, step_time, last_step, shots, level, highscore, game_state
    global blocks_cleared_total, next_level_blocks
    global weapon_slow_until, weapon_mono_until, weapon_mono_color, weapon_rocket_charges

    print("GAME OVER!", reason, "Finaler Score:", score)

    last_score = score
    if score > highscore:
        highscore = score
        save_highscore()
        show_effect("NEW HIGHSCORE!", 2.0)
    else:
        show_effect("GAME OVER", 1.5)

    play_game_over()
    sound_update(time.monotonic())

    for _ in range(3):
        pixels.fill((255, 0, 0))
        pixels.show()
        time.sleep(0.2)
        pixels.fill((0, 0, 0))
        pixels.show()
        time.sleep(0.2)

    strip_state = [None] * NUM_LEDS
    shots = []

    score = 0
    lives = 3
    level = 1
    blocks_cleared_total = 0
    next_level_blocks = speedup_every
    step_time = 1.5
    last_step = time.monotonic()
    game_state = "GAME_OVER"

    weapon_slow_until = 0.0
    weapon_mono_until = 0.0
    weapon_mono_color = None
    weapon_rocket_charges = 0

    draw_strip()
    update_display()


def start_new_game():
    """Spielzustand für einen neuen Run initialisieren."""
    global strip_state, shots, score, last_score, lives, level, step_time, last_step, last_shot_step, game_state
    global blocks_cleared_total, next_level_blocks
    global weapon_slow_until, weapon_mono_until, weapon_mono_color, weapon_rocket_charges

    strip_state = [None] * NUM_LEDS
    shots = []
    score = 0
    last_score = 0
    lives = 3
    level = 1
    blocks_cleared_total = 0
    next_level_blocks = speedup_every
    step_time = 1.5
    last_step = time.monotonic()
    last_shot_step = time.monotonic()
    game_state = "RUNNING"

    weapon_slow_until = 0.0
    weapon_mono_until = 0.0
    weapon_mono_color = None
    weapon_rocket_charges = 0

    draw_strip()
    update_display()
    show_effect("READY!", 0.7)
    play_start_new_game()


# =========================================
#              STARTSETUP
# =========================================

load_highscore()
update_display()


# =========================================
#                 HAUPTSCHLEIFE
# =========================================

while True:
    now = time.monotonic()

    # Sound-Sequenzen non-blocking updaten
    sound_update(now)

    # Effekttext automatisch ausblenden
    if effect_text and now > effect_timer:
        effect_text = None
        update_display()

    # === Schüsse: schneller Takt ===
    if game_state == "RUNNING" and now - last_shot_step >= shot_step_time:
        last_shot_step = now
        update_shots_and_collisions()
        draw_strip()

    # === Blöcke: langsamerer Takt (mit Slow-Powerup) ===
    current_step_time = step_time
    if now < weapon_slow_until:
        current_step_time = step_time * 1.5  # 50% langsamer

    if game_state == "RUNNING" and now - last_step >= current_step_time:
        last_step = now
        shift_down()

        if strip_state[0] is not None:
            game_over("Block hat den Boden erreicht")
            continue

        draw_strip()

    # Button-Handling (Flankenerkennung)
    pressed_index = None
    for idx, btn in enumerate(buttons):
        current = btn.value  # True = nicht gedrückt, False = gedrückt
        if prev_button_state[idx] and not current:
            pressed_index = idx
        prev_button_state[idx] = current

    if pressed_index is not None:
        if game_state in ("START", "GAME_OVER"):
            start_new_game()
        elif game_state == "RUNNING":
            color_idx = pressed_index
            now = time.monotonic()
            if now < weapon_mono_until and weapon_mono_color is not None:
                color_idx = weapon_mono_color
            shots.append((color_idx, 0))
            play_shot()
            print("Schuss! Farbe:", color_idx, "Shots:", shots)
            draw_strip()

    time.sleep(0.005)