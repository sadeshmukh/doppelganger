import io

from ziamath import zmath as zm
import cairosvg


def gen_latex_png(text: str, bg: str = "white", height: int = 400) -> io.BytesIO:
    svg = zm.Latex(f"{text.strip("$ ")}").svg()
    png = cairosvg.svg2png(bytestring=svg, background_color=bg, output_height=height)
    if not png:
        raise ValueError("guh: " + text)
    return io.BytesIO(png)


if __name__ == "__main__":
    with open("t.png", "wb") as f:
        f.write(gen_latex_png(r"\frac{1}{2}").getbuffer())
