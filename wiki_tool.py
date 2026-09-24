import os
import sys
import argparse
import unicodedata
import httpx
import time
import matplotlib.pyplot as plt
from datetime import datetime
from dotenv import load_dotenv
from fpdf import FPDF, XPos, YPos
from google import genai
from google.genai.errors import ServerError

load_dotenv()

USER_AGENT = os.getenv("WT_USER_AGENT", "wikiscout/1.0 (admin@example.com)")
HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}


def fetch_pageviews(project: str, article: str, start: str, end: str) -> list[dict]:
    # Заміна пробілів на підкреслення для формату Вікіпедії
    safe_article = article.replace(" ", "_")
    url = f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{project}/all-access/user/{safe_article}/daily/{start}/{end}"
    resp = httpx.get(url, headers=HEADERS, timeout=15)
    if resp.status_code == 404:
        return []  # Статтю не знайдено
    resp.raise_for_status()
    return resp.json().get("items", [])


def generate_insights_with_gemini(client: genai.Client, stats: dict) -> str:
    prompt = f"""
    You are a B2C product growth analyst. Analyze the following Wikipedia pageview traffic data:
    {stats}

    Provide an executive summary for a startup founder (max 120 words):
    1. Compare the audience demand and trends across the provided segments.
    2. Note data reliability or significant anomalies (spikes).
    3. Actionable Founder Recommendation: which segment to prioritize for a B2C product launch and why.

    Write strictly in English plain text. Do NOT use markdown bolding (no **), bullet points (*), or special symbols.
    """

    max_retries = 3
    delay = 2

    for attempt in range(max_retries):
        try:
            chat = client.chats.create(model="gemini-2.5-flash")
            response = chat.send_message(prompt)

            clean_text = (
                response.text.strip()
                .replace("**", "")
                .replace("—", "-")
                .replace("“", '"')
                .replace("”", '"')
                .replace("’", "'")
            )
            return clean_text

        except ServerError as e:
            if e.code == 503 and attempt < max_retries - 1:
                print(f" (Gemini API 503 Overloaded. Retrying in {delay}s...)")
                time.sleep(delay)
                delay *= 2
            else:
                raise e

def safe_pdf_text(text: str) -> str:
    """Транслітерує кирилицю та нормалізує європейські символи (знімає діакритику) для FPDF."""
    # 1. Транслітерація кирилиці
    cyrillic = "АБВГДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯабвгдеєжзиіїйклмнопрстуфхцчшщьюя"
    latin = ["A", "B", "V", "G", "D", "E", "Ye", "Zh", "Z", "Y", "I", "Yi", "Y", "K", "L", "M", "N", "O", "P", "R", "S",
             "T", "U", "F", "Kh", "Ts", "Ch", "Sh", "Shch", "'", "Yu", "Ya",
             "a", "b", "v", "g", "d", "e", "ye", "zh", "z", "y", "i", "yi", "y", "k", "l", "m", "n", "o", "p", "r", "s",
             "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "'", "yu", "ya"]
    trans_dict = dict(zip(cyrillic, latin))
    res = "".join(trans_dict.get(c, c) for c in text)

    # 2. Нормалізація європейських літер (видалення умлаутів, акцентів тощо)
    res = ''.join(c for c in unicodedata.normalize('NFD', res) if unicodedata.category(c) != 'Mn')

    return res.encode("latin-1", "replace").decode("latin-1")

def build_pdf_report(datasets: dict, summary_text: str, filename="report.pdf") -> str:
    chart_path = "temp_chart.png"
    plt.figure(figsize=(8, 4))

    for label, items in datasets.items():
        if not items:
            continue
        dates = [datetime.strptime(x["timestamp"], "%Y%m%d00") for x in items]
        views = [x["views"] for x in items]
        plt.plot(dates, views, linewidth=1.5, label=label)

    plt.title("Wikipedia Market Trends Comparison")
    plt.grid(True, linestyle="--", alpha=0.5)
    plt.legend()
    plt.tight_layout()

    # Використовуємо кирилицю на графіку, але для PDF-тексту робимо транслітерацію
    plt.savefig(chart_path, dpi=150)
    plt.close()

    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, "Market Trend Report (Comparative)", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font("Helvetica", "", 9)
    for label, items in datasets.items():
        safe_label = safe_pdf_text(label)  # Очищаємо назву від кирилиці для тексту PDF
        if not items:
            pdf.cell(0, 6, f"{safe_label}: No data found", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
            continue
        views = [x["views"] for x in items]
        total = sum(views)
        avg = total // len(views)
        pdf.cell(0, 6, f"Source: {safe_label} | Total: {total:,} | Daily Avg: {avg:,}", new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(3)
    pdf.image(chart_path, x=10, w=190)
    pdf.ln(5)

    pdf.set_font("Helvetica", "B", 12)
    pdf.cell(0, 8, "Executive Summary & Agent Insights:", new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(2)

    pdf.set_font("Helvetica", "", 10)
    safe_summary = safe_pdf_text(summary_text)
    pdf.multi_cell(0, 6, safe_summary)

    pdf.output(filename)
    if os.path.exists(chart_path):
        os.remove(chart_path)
    return filename


def main():
    parser = argparse.ArgumentParser(description="Wikipedia Trends Agent Skill")
    parser.add_argument("--queries", nargs="+", required=True,
                        help="List of project:article (e.g. uk.wikipedia:Python pl.wikipedia:Python)")
    parser.add_argument("--start", required=True, help="Start date YYYYMMDD")
    parser.add_argument("--end", required=True, help="End date YYYYMMDD")
    parser.add_argument("--output", default="report.pdf", help="Output PDF file")
    args = parser.parse_args()

    client = genai.Client()
    datasets = {}
    stats_for_ai = {}

    print("1. Fetching pageviews...")
    for query in args.queries:
        try:
            project, article = query.split(":", 1)
        except ValueError:
            print(f"Error: Invalid query format '{query}'. Expected 'project:article'")
            sys.exit(1)

        items = fetch_pageviews(project, article, args.start, args.end)
        datasets[query] = items

        if items:
            views = [x["views"] for x in items]
            stats_for_ai[query] = {
                "total": sum(views),
                "daily_avg": sum(views) // len(views),
                "peak": max(views)
            }
        else:
            stats_for_ai[query] = "No data"

    if not any(datasets.values()):
        print("Error: No traffic data retrieved for any queries.")
        sys.exit(1)

    print("2. Generating AI insights with Gemini...")
    insights = generate_insights_with_gemini(client, stats_for_ai)

    print("3. Building PDF report...")
    pdf_path = build_pdf_report(datasets, insights, args.output)
    print(f"\nDone! Report generated at: {pdf_path}")


if __name__ == "__main__":
    main()
