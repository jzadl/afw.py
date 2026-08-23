"""
fireworks.py: terminal fireworks with sub-pixel rendering and true color.

Controls:
    space     - launch a firework
    1-5       - switch color palettes
    a         - toggle auto-launch
    q / ESC   - quit
"""

from __future__ import annotations

import math
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import afw

GRAVITY = 30.0
PARTICLE_LIFE = 1.8
LAUNCH_SPEED = 55.0

PALETTES = {
    "fire": [
        (255, 80, 20), (255, 160, 40), (255, 220, 80),
        (255, 50, 10), (200, 30, 10),
    ],
    "ocean": [
        (40, 180, 255), (80, 220, 255), (120, 255, 255),
        (20, 120, 255), (0, 80, 200),
    ],
    "neon": [
        (255, 0, 255), (0, 255, 255), (255, 255, 0),
        (0, 255, 128), (255, 128, 0),
    ],
    "sakura": [
        (255, 180, 200), (255, 140, 170), (255, 200, 220),
        (255, 100, 150), (255, 220, 240),
    ],
    "toxic": [
        (0, 255, 64), (128, 255, 0), (200, 255, 0),
        (0, 200, 32), (64, 255, 32),
    ],
}

PALETTE_NAMES = list(PALETTES.keys())


class Particle:
    __slots__ = ("x", "y", "vx", "vy", "r", "g", "b", "life", "max_life")

    def __init__(self, x, y, vx, vy, r, g, b, life):
        self.x = x
        self.y = y
        self.vx = vx
        self.vy = vy
        self.r = r
        self.g = g
        self.b = b
        self.life = life
        self.max_life = life


class Firework:
    def __init__(self, x, y, target_y, palette_name):
        self.x = x
        self.y = y
        self.target_y = target_y
        self.palette = PALETTES[palette_name]
        self.exploded = False
        self.vy = -LAUNCH_SPEED
        self.trail: list[Particle] = []
        self.particles: list[Particle] = []

    def update(self, dt: float) -> bool:
        if not self.exploded:
            self.y += self.vy * dt
            self.vy += GRAVITY * dt * 0.3
            if random.random() < 0.7:
                self.trail.append(Particle(
                    self.x + random.uniform(-0.3, 0.3), self.y,
                    random.uniform(-2, 2), random.uniform(2, 8),
                    255, 200, 100, 0.4,
                ))
            if self.y <= self.target_y or self.vy >= 0:
                self._explode()

        for p in self.trail:
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.life -= dt
        self.trail = [p for p in self.trail if p.life > 0]

        for p in self.particles:
            p.x += p.vx * dt
            p.y += p.vy * dt
            p.vy += GRAVITY * dt
            p.vx *= 0.99
            p.life -= dt
        self.particles = [p for p in self.particles if p.life > 0]

        return bool(self.particles or self.trail or not self.exploded)

    def _explode(self):
        self.exploded = True
        count = random.randint(60, 120)
        base_angle = random.uniform(0, 2 * math.pi)
        for i in range(count):
            angle = base_angle + (i / count) * 2 * math.pi + random.uniform(-0.2, 0.2)
            speed = random.uniform(15, 55)
            r, g, b = random.choice(self.palette)
            r = max(0, min(255, r + random.randint(-30, 30)))
            g = max(0, min(255, g + random.randint(-30, 30)))
            b = max(0, min(255, b + random.randint(-30, 30)))
            self.particles.append(Particle(
                self.x, self.y,
                math.cos(angle) * speed, math.sin(angle) * speed,
                r, g, b, PARTICLE_LIFE * random.uniform(0.6, 1.0),
            ))
        for _ in range(20):
            angle = random.uniform(0, 2 * math.pi)
            speed = random.uniform(5, 20)
            self.particles.append(Particle(
                self.x, self.y,
                math.cos(angle) * speed, math.sin(angle) * speed,
                255, 255, 255, 0.3,
            ))


class Star:
    def __init__(self, x, y):
        self.x = x
        self.y = y
        self.brightness = random.uniform(0.2, 0.8)
        self.speed = random.uniform(1.5, 4.0)
        self.phase = random.uniform(0, 2 * math.pi)

    def get_brightness(self, t: float) -> float:
        return self.brightness * (0.6 + 0.4 * math.sin(t * self.speed + self.phase))


class Game:
    def __init__(self):
        self.fireworks: list[Firework] = []
        self.stars: list[Star] = []
        self.palette_idx = 0
        self.auto_launch = True
        self.time = 0.0
        self.auto_timer = 0.0
        self.w = 80
        self.h = 40

    def palette_name(self) -> str:
        return PALETTE_NAMES[self.palette_idx]

    def launch(self, tx: float = None, ty: float = None):
        if tx is None:
            tx = random.uniform(5, self.w - 5)
        if ty is None:
            ty = random.uniform(3, self.h * 0.4)
        self.fireworks.append(Firework(tx, self.h + 2, ty, self.palette_name()))

    def spawn_stars(self):
        self.stars = [Star(random.uniform(0, self.w), random.uniform(0, self.h * 0.6)) for _ in range(50)]

    def handle_key(self, ev: afw.KeyEvent):
        if ev.key == afw.Key.ESCAPE or ev.is_char("q") or ev.key == afw.Key.CTRL_C:
            return False
        if ev.is_char(" "):
            self.launch()
        if ev.is_char("a"):
            self.auto_launch = not self.auto_launch
        for i, name in enumerate(PALETTE_NAMES):
            if ev.is_char(str(i + 1)):
                self.palette_idx = i
        return True

    def handle_mouse(self, ev: afw.MouseEvent):
        if ev.button == "left" and ev.pressed:
            self.launch(ev.x, ev.y)
        return True

    def update(self, dt: float):
        self.time += dt

        if self.auto_launch:
            self.auto_timer += dt
            if self.auto_timer > 1.8:
                self.auto_timer = 0.0
                if len(self.fireworks) < 5:
                    self.launch()

        self.fireworks = [fw for fw in self.fireworks if fw.update(dt)]

    def render(self, canvas: afw.Canvas):
        self.w = canvas.width
        self.h = canvas.height

        if not self.stars:
            self.spawn_stars()

        canvas.clear(afw.Color(5, 5, 15))

        for star in self.stars:
            b = int(star.get_brightness(self.time) * 255)
            sx = int(star.x) % canvas.width
            sy = int(star.y) % canvas.height
            canvas.put_char(sx, sy, ".", afw.Style(fg=afw.Color(b, b, b)))

        for fw in self.fireworks:
            for p in fw.trail:
                if p.life > 0:
                    a = p.life / p.max_life
                    r, g, b = int(p.r * a), int(p.g * a), int(p.b * a)
                    y2 = int(p.y * 2)
                    x = int(p.x)
                    if 0 <= x < canvas.width and 0 <= y2 < canvas.height * 2:
                        canvas.put_subpixel(x, y2, afw.Color(r, g, b))
                        canvas.put_subpixel(x, y2 + 1, afw.Color(r // 3, g // 3, b // 3))

            for p in fw.particles:
                if p.life > 0:
                    a = p.life / p.max_life
                    r, g, b = int(p.r * a), int(p.g * a), int(p.b * a)
                    y2 = int(p.y * 2)
                    x = int(p.x)
                    if 0 <= x < canvas.width and 0 <= y2 < canvas.height * 2:
                        canvas.put_subpixel(x, y2, afw.Color(r, g, b))
                        if a > 0.3:
                            gr = min(255, r + 40)
                            gg = min(255, g + 40)
                            gb = min(255, b + 40)
                            canvas.put_subpixel(x, y2 + 1, afw.Color(gr, gg, gb))
                        else:
                            canvas.put_subpixel(x, y2 + 1, afw.Color(r // 2, g // 2, b // 2))

        palette = self.palette_name()
        auto = "ON" if self.auto_launch else "OFF"
        hud = f" [{palette}] auto={auto} | space=launch 1-5=palette a=auto q=quit "
        canvas.draw_text(0, 0, hud, afw.Style(fg=afw.Color(180, 180, 180), bg=afw.Color(20, 20, 30)))

        count = sum(1 for fw in self.fireworks for _ in fw.particles)
        stats = f" particles: {count} "
        canvas.draw_text(canvas.width - len(stats), 0, stats, afw.Style(fg=afw.Color(100, 200, 255), bg=afw.Color(20, 20, 30)))


def main():
    app = afw.App(target_fps=60, mouse=True, show_fps=True)
    game = Game()

    @app.on_key
    def handle_key(ev):
        if not game.handle_key(ev):
            app.stop()

    @app.on_mouse
    def handle_mouse(ev):
        game.handle_mouse(ev)

    @app.on_update
    def update(dt):
        game.update(dt)

    @app.on_render
    def render(canvas):
        game.render(canvas)

    app.run()


if __name__ == "__main__":
    main()
