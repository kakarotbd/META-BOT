"""Image rendering utilities for the XP leaderboard feature."""
import concurrent.futures
import io
import urllib.request

from PIL import Image, ImageDraw, ImageFont, ImageOps
from config.app_config import APP_CONFIG


def _leaderboard_font(size, bold=False):
    candidates = ([r"C:\Windows\Fonts\seguisb.ttf", r"C:\Windows\Fonts\segoeui.ttf"] if bold else [r"C:\Windows\Fonts\segoeui.ttf"])
    candidates += (["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"] if bold else ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"])
    for path in candidates:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


class _ScaledImageDraw:
    """Draw using logical coordinates while rasterizing at a higher resolution."""

    def __init__(self, draw, scale):
        self.draw, self.scale = draw, scale

    def _box(self, box):
        return tuple(round(value*self.scale) for value in box)

    def rounded_rectangle(self, box, **kwargs):
        kwargs["radius"] = round(kwargs.get("radius", 0)*self.scale)
        kwargs["width"] = round(kwargs.get("width", 1)*self.scale)
        return self.draw.rounded_rectangle(self._box(box), **kwargs)

    def ellipse(self, box, **kwargs):
        kwargs["width"] = round(kwargs.get("width", 1)*self.scale)
        return self.draw.ellipse(self._box(box), **kwargs)

    def _font(self, font):
        return font

    def text(self, xy, text, **kwargs):
        return self.draw.text(self._box(xy), text, font=self._font(kwargs.pop("font", None)), **kwargs)

    def textbbox(self, xy, text, **kwargs):
        box = self.draw.textbbox(self._box(xy), text, font=self._font(kwargs.pop("font", None)), **kwargs)
        return tuple(round(value/self.scale) for value in box)


def _fetch_leaderboard_image(url):
    if not url:
        return None
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MetaBot-Leaderboard/1.0"})
        with urllib.request.urlopen(request, timeout=5) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            return None
        with Image.open(io.BytesIO(data)) as image:
            return image.convert("RGBA")
    except Exception:
        return None


def render_leaderboard_png(guild_name, icon_url, entries):
    """Render the top ten lifetime-XP users into a polished Discord PNG card."""
    scale = 2
    width, height = 960, 920
    image = Image.new("RGBA", (width*scale, height*scale), "#090e19")
    draw = _ScaledImageDraw(ImageDraw.Draw(image), scale)
    draw.rounded_rectangle((22, 22, width-22, height-22), radius=26, fill="#101827", outline="#263650", width=2)
    draw.rounded_rectangle((42, 42, width-42, 210), radius=20, fill="#17243a")
    draw.rounded_rectangle((62, 62, 182, 182), radius=16, fill="#273650", outline="#7185a8", width=2)
    draw.text((218, 68), f"{APP_CONFIG.bot_app_name.upper()} LEADERBOARD", font=_leaderboard_font(19, True), fill="#a8b8d8")
    draw.text((218, 101), str(guild_name)[:30], font=_leaderboard_font(34, True), fill="#f5f7ff")
    draw.text((218, 158), "TOP 10  |  RANKED BY TOTAL XP", font=_leaderboard_font(15, True), fill="#a3b5d4")
    images = [icon_url] + [entry.get("avatar_url") for entry in entries]
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        fetched = list(pool.map(_fetch_leaderboard_image, images))
    if fetched and fetched[0]:
        icon = ImageOps.fit(fetched[0], (112*scale, 112*scale), centering=(0.5, 0.5))
        mask = Image.new("L", (112*scale,112*scale), 0); ImageDraw.Draw(mask).rounded_rectangle((0,0,111*scale,111*scale), radius=13*scale, fill=255)
        image.paste(icon, (66*scale,66*scale), mask)
    body_font = _leaderboard_font(19*scale)
    small_font = _leaderboard_font(15*scale)
    bold_font = _leaderboard_font(20*scale, True)
    rank_colors = ["#f3c969", "#c8d3e8", "#d89468"]
    row_start, row_height = 226, 62
    for index, entry in enumerate(entries[:10]):
        y = row_start + index * row_height
        fill = "#1b2941" if index % 2 == 0 else "#162238"
        draw.rounded_rectangle((48,y,width-48,y+54), radius=14, fill=fill)
        rank_color = rank_colors[index] if index < 3 else "#8798b9"
        draw.rounded_rectangle((59,y+8,99,y+46), radius=11, fill="#293750")
        rank_text = str(index+1)
        rank_box = draw.textbbox((0,0),rank_text,font=bold_font)
        draw.text((79-(rank_box[2]-rank_box[0])/2,y+15),rank_text,font=bold_font,fill=rank_color)
        avatar = fetched[index+1] if index+1 < len(fetched) else None
        if avatar:
            avatar = ImageOps.fit(avatar,(40*scale,40*scale),centering=(0.5,0.5))
            mask = Image.new("L",(40*scale,40*scale),0); ImageDraw.Draw(mask).ellipse((0,0,39*scale,39*scale),fill=255)
            image.paste(avatar,(115*scale,(y+7)*scale),mask)
        else:
            draw.ellipse((115,y+7,155,y+47),fill="#344866")
            initials = (entry.get("name") or "?").strip()[:1].upper()
            draw.text((128,y+16),initials,font=small_font,fill="#dce6ff")
        draw.text((172,y+7),(entry.get("name") or "Unknown member")[:24],font=body_font,fill="#f2f5ff")
        draw.text((173,y+32),f"LEVEL {int(entry.get('level',1)):,}",font=small_font,fill="#a9bad8")
        total_text=f"{int(entry.get('total_xp',0)):,} XP"
        text_box=draw.textbbox((0,0),total_text,font=bold_font)
        draw.text((width-65-(text_box[2]-text_box[0]),y+17),total_text,font=bold_font,fill="#c2adff")
    draw.text((52,height-56),"Keep earning XP to climb the ranks",font=small_font,fill="#879ab9")
    output=io.BytesIO(); image.convert("RGB").save(output,format="PNG",optimize=True)
    output.seek(0)
    return output

