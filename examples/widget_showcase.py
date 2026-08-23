import sys, time, math, random
sys.path.insert(0, "/home/jzadl/Downloads/afw3")

import afw


def setup(canvas):
    global scene, title, subtitle, score_box, progress, power_bar
    global sprite_w, sprite_b, bg_stars, elapsed, anim

    scene = afw.Scene()
    elapsed = 0.0
    anim = scene.animations

    # --- TITLE (TextWidget, centered) ---
    title = afw.TextWidget(
        canvas.width // 2, 1,
        "AFW WIDGET SHOWCASE",
        afw.Style(fg=afw.Color.hex("#ffcc00"), bold=True),
        anchor="center",
    )
    scene.add(title)

    # --- SUBTITLE (TextWidget, animated fade) ---
    subtitle = afw.TextWidget(
        canvas.width // 2, 3,
        "sub-pixel rendering + real tweens",
        afw.Style(fg=afw.Color.hex("#888888"), italic=True),
        anchor="center",
    )
    scene.add(subtitle)

    # --- SCORE BOX (BoxWidget + nested TextWidget) ---
    score_box = afw.BoxWidget(2, 5, 24, 4, afw.Style(fg=afw.Color.hex("#00ccff")))
    scene.add(score_box)

    score_label = afw.TextWidget(4, 6, "SCORE", afw.Style(fg=afw.Color.hex("#00ccff"), bold=True))
    scene.add(score_label)

    score_val = afw.TextWidget(4, 8, "0", afw.Style(fg=afw.Color.hex("#ffffff"), bold=True))
    scene.add(score_val)

    # --- PROGRESS BAR ---
    progress_label = afw.TextWidget(30, 5, "LOADING", afw.Style(fg=afw.Color.hex("#aaaaaa")))
    scene.add(progress_label)

    progress = afw.ProgressBarWidget(
        30, 7, 20, 1,
        value=0.0, max_value=100.0,
        style=afw.Style(fg=afw.Color.hex("#00ff88")),
        fill_char="\u2588", empty_char="\u2591",
    )
    scene.add(progress)

    # --- POWER BAR ---
    power_label = afw.TextWidget(55, 5, "POWER", afw.Style(fg=afw.Color.hex("#ff5555")))
    scene.add(power_label)

    power_bar = afw.ProgressBarWidget(
        55, 7, 20, 1,
        value=0.0, max_value=100.0,
        style=afw.Style(fg=afw.Color.hex("#ff4444")),
        fill_char="\u2503", empty_char="\u2502",
    )
    scene.add(power_bar)

    # --- SPRITE WIDGETS ---
    ship_art = (
        "  /\\\n"
        " /  \\\n"
        "/ AB \\\n"
        "\\ AB /\n"
        " \\  /\n"
        "  \\/"
    )
    ship_sprite = afw.Sprite.from_text(
        ship_art,
        afw.Style(fg=afw.Color.hex("#ff6600"), bold=True),
        palette={
            "A": afw.Style(fg=afw.Color.hex("#ff0000")),
            "B": afw.Style(fg=afw.Color.hex("#ffaa00")),
        },
    )
    sprite_w = afw.SpriteWidget(canvas.width // 2 - 3, 12, ship_sprite)
    scene.add(sprite_w)

    star_art = "*"
    star_sprite = afw.Sprite.from_text(star_art, afw.Style(fg=afw.Color.hex("#ffff55"), bold=True))
    sprite_b = afw.SpriteWidget(canvas.width // 2 + 6, 12, star_sprite)
    scene.add(sprite_b)

    # --- STATUS BOX (BoxWidget bottom panel) ---
    status_box = afw.BoxWidget(2, canvas.height - 5, canvas.width - 4, 3, afw.Style(fg=afw.Color.hex("#444444")))
    scene.add(status_box)

    status = afw.TextWidget(
        4, canvas.height - 4,
        "widgets: Text, Box, Sprite, ProgressBar  |  scene: children list + AnimationManager  |  arrow keys animate ship",
        afw.Style(fg=afw.Color.hex("#888888")),
    )
    scene.add(status)

    # --- BACKGROUND STARS ---
    bg_stars = []
    for _ in range(30):
        sx = random.randint(0, canvas.width - 1)
        sy = random.randint(4, canvas.height - 6)
        c = random.choice([".", "*", "`"])
        bright = random.randint(100, 200)
        star = afw.TextWidget(sx, sy, c, afw.Style(fg=afw.Color(bright, bright, bright)))
        scene.add(star)
        bg_stars.append(star)

    # --- TWINKLE ANIMATION on subtitle ---
    anim.animate(subtitle, "opacity", 0.0, 3.0, loops=-1, yoyo=True, easing=afw.Easing.ease_in_out_sine)


def frame(canvas, dt):
    global elapsed
    elapsed += dt

    # progress bar fills and loops
    progress.value = (elapsed * 20) % 100

    # power bar oscillates
    power_bar.value = (math.sin(elapsed * 2.0) + 1.0) * 50.0

    # ship floats side to side
    sprite_w.offset_x = math.sin(elapsed * 1.5) * 4.0
    sprite_w.offset_y = math.cos(elapsed * 0.8) * 1.5

    # star orbits the ship
    orbit_x = math.cos(elapsed * 3.0) * 4.0
    orbit_y = math.sin(elapsed * 2.0) * 2.0
    sprite_b.offset_x = sprite_w.offset_x + orbit_x + 6
    sprite_b.offset_y = sprite_w.offset_y + orbit_y

    # background stars twinkle
    for i, star in enumerate(bg_stars):
        phase = elapsed * (0.5 + i * 0.1) + i * 1.7
        b = int((math.sin(phase) + 1.0) * 0.5 * 200)
        star.style = afw.Style(fg=afw.Color(b, b, b))

    # update scene (animations + tickers)
    scene.update(dt)

    # draw everything
    canvas.clear(afw.Color(8, 8, 18))
    scene.draw(canvas)


if __name__ == "__main__":
    app = afw.App("Widget Showcase", mouse=False)
    app.run(setup, frame)
