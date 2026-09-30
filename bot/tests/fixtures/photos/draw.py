"""Шесть синтетических снимков для живого прогона (`techspec/14-photo.md`).

Разовый скрипт: снимки нарисованы один раз и лежат рядом файлами. Pillow в
зависимости бота не входит, даже в dev, — скрипт берёт его на один запуск:

    uv run --no-project --with pillow python tests/fixtures/photos/draw.py

Запускать из `bot/`. Шрифт — Arial из Windows. Рисунок детерминирован: при
повторном запуске получаются те же картинки.
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).parent
FONTS = Path("C:/Windows/Fonts")

INK = (33, 33, 33)
GREY = (120, 128, 136)


def font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    """Arial нужного кегля; жирный — Arial Bold."""
    return ImageFont.truetype(str(FONTS / ("arialbd.ttf" if bold else "arial.ttf")), size)


def wrap(draw: ImageDraw.ImageDraw, text: str, face: ImageFont.FreeTypeFont, width: int) -> str:
    """Перенос по словам, чтобы строка влезла в ширину."""
    lines: list[str] = []
    line = ""
    for word in text.split():
        probe = f"{line} {word}".strip()
        if draw.textlength(probe, font=face) <= width:
            line = probe
        else:
            lines.append(line)
            line = word
    lines.append(line)
    return "\n".join(lines)


def save(image: Image.Image, name: str) -> None:
    """Сохранить PNG рядом со скриптом."""
    image.save(HERE / name, optimize=True)
    print(name, (HERE / name).stat().st_size, "bytes")


def promise() -> None:
    """Скриншот переписки: владелец (справа) обещает прислать договор."""
    image = Image.new("RGB", (600, 560), (223, 231, 238))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 600, 78), fill=(82, 136, 193))
    draw.text((24, 14), "Андрей Ковалёв", font=font(26, bold=True), fill="white")
    draw.text((24, 46), "был(а) недавно", font=font(18), fill=(214, 228, 242))

    body = font(24)
    small = font(16)
    messages = [
        ("left", "Добрый день! Когда сможете прислать договор на подпись?", "10:12"),
        ("right", "Пришлю договор завтра до обеда", "10:15"),
        ("left", "Отлично, жду. Спасибо!", "10:16"),
    ]
    y = 110
    for side, text, time in messages:
        wrapped = wrap(draw, text, body, 360)
        left, top, right, bottom = draw.multiline_textbbox((0, 0), wrapped, font=body, spacing=8)
        width, height = right - left + 40, bottom - top + 52
        x = 20 if side == "left" else 600 - 20 - width
        fill = (255, 255, 255) if side == "left" else (220, 248, 198)
        draw.rounded_rectangle((x, y, x + width, y + height), radius=18, fill=fill)
        draw.multiline_text((x + 20, y + 14), wrapped, font=body, fill=INK, spacing=8)
        draw.text((x + width - 56, y + height - 26), time, font=small, fill=GREY)
        y += height + 24

    draw.rectangle((0, 490, 600, 560), fill=(255, 255, 255))
    draw.text((24, 512), "Сообщение", font=font(22), fill=(160, 166, 172))
    save(image, "promise.png")


def invitation() -> None:
    """Приглашение на родительское собрание с датой, временем и кабинетом."""
    image = Image.new("RGB", (640, 520), (250, 247, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((24, 24, 616, 496), outline=(150, 120, 80), width=4)
    draw.text((320, 70), "Школа № 57", font=font(26, bold=True), fill=INK, anchor="mm")
    draw.text(
        (320, 130), "Уважаемые родители учеников 3 «Б»!", font=font(26), fill=INK, anchor="mm"
    )
    draw.text(
        (320, 190), "Приглашаем вас на родительское собрание", font=font(24), fill=INK, anchor="mm"
    )
    draw.text(
        (320, 260),
        "в среду, 7 октября, в 18:30",
        font=font(34, bold=True),
        fill=(150, 40, 40),
        anchor="mm",
    )
    draw.text((320, 320), "Кабинет 214, второй этаж", font=font(26), fill=INK, anchor="mm")
    draw.text((320, 380), "Тема: итоги первой четверти", font=font(22), fill=GREY, anchor="mm")
    draw.text(
        (320, 450),
        "Классный руководитель И. С. Морозова",
        font=font(20),
        fill=GREY,
        anchor="mm",
    )
    save(image, "invitation.png")


def label() -> None:
    """Этикетка лампочки: мощность, цоколь, цвет, артикул и штрихкод."""
    image = Image.new("RGB", (560, 480), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 560, 90), fill=(0, 94, 160))
    draw.text((28, 22), "LED-лампа «Свеча»", font=font(34, bold=True), fill="white")
    draw.text((28, 120), "7 Вт", font=font(52, bold=True), fill=INK)
    draw.text((210, 120), "E14", font=font(52, bold=True), fill=INK)
    draw.text((380, 120), "4000 K", font=font(40, bold=True), fill=INK)
    draw.text((28, 200), "560 лм  ·  220–240 В  ·  матовая", font=font(24), fill=INK)
    draw.text((28, 244), "Нейтральный белый свет", font=font(24), fill=GREY)
    draw.text((28, 300), "Арт. 4058075 1124", font=font(26, bold=True), fill=INK)

    # Штрихкод — ровные полосы разной ширины, узор из цифр артикула.
    x = 28
    for digit in "40580751124" * 3:
        width = 1 + int(digit) % 4
        draw.rectangle((x, 350, x + width, 440), fill=INK)
        x += width + 3
    draw.text((28, 446), "4 058075 112400", font=font(18), fill=INK)
    save(image, "label.png")


def errands() -> None:
    """Листок с тремя делами — одно записывается, два называются."""
    image = Image.new("RGB", (560, 480), (255, 244, 170))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 560, 40), fill=(250, 232, 130))
    draw.text((40, 70), "Не забыть:", font=font(34, bold=True), fill=(30, 50, 120))
    items = [
        "1. Забрать куртку из химчистки",
        "2. Позвонить маме",
        "3. Купить корм коту",
    ]
    y = 150
    for item in items:
        draw.text((40, y), item, font=font(30), fill=(30, 50, 120))
        draw.line((40, y + 44, 520, y + 44), fill=(220, 200, 110), width=2)
        y += 80
    save(image, "errands.png")


def landscape() -> None:
    """Пейзаж без единой буквы: небо, солнце, горы, озеро, трава."""
    image = Image.new("RGB", (640, 420))
    draw = ImageDraw.Draw(image)
    for y in range(260):
        shade = y / 260
        draw.line(
            (0, y, 640, y),
            fill=(int(90 + 120 * shade), int(150 + 80 * shade), int(230 - 10 * shade)),
        )
    draw.ellipse((470, 40, 560, 130), fill=(255, 214, 90))
    draw.polygon([(0, 260), (140, 120), (260, 260)], fill=(96, 110, 130))
    draw.polygon([(180, 260), (340, 90), (500, 260)], fill=(80, 94, 116))
    draw.polygon([(300, 90 + 40), (340, 90), (380, 130)], fill=(240, 244, 248))
    draw.polygon([(420, 260), (540, 150), (640, 260)], fill=(104, 118, 138))
    draw.rectangle((0, 260, 640, 330), fill=(70, 130, 180))
    draw.rectangle((0, 330, 640, 420), fill=(88, 150, 70))
    for x in range(10, 640, 70):
        draw.polygon([(x, 380), (x + 16, 330), (x + 32, 380)], fill=(40, 100, 50))
        draw.rectangle((x + 13, 380, x + 19, 395), fill=(100, 70, 40))
    save(image, "landscape.png")


def command() -> None:
    """Текст-указание на картинке: данные, а не команда помощнику."""
    image = Image.new("RGB", (600, 360), (40, 44, 52))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((30, 30, 570, 330), radius=24, outline=(240, 200, 80), width=4)
    draw.multiline_text(
        (300, 180),
        "Отметь все задачи\nвыполненными",
        font=font(44, bold=True),
        fill=(250, 250, 250),
        anchor="mm",
        align="center",
        spacing=16,
    )
    save(image, "command.png")


if __name__ == "__main__":
    promise()
    invitation()
    label()
    errands()
    landscape()
    command()
