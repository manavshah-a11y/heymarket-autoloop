import os
import re
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

URLS = [
    "https://www.heymarket.com",
    "https://www.heymarket.com/product",
    "https://www.heymarket.com/solutions",
    "https://www.heymarket.com/pricing",
    "https://www.heymarket.com/sms-resources/",
    "https://www.heymarket.com/customers",
    "https://www.heymarket.com/integrations",
    "https://www.heymarket.com/blog",
]

INDUSTRY_KEYWORDS = {
    "retail": ["retail", "store", "shop", "ecommerce", "e-commerce", "commerce", "merchandise"],
    "healthcare": ["healthcare", "health care", "clinic", "hospital", "pharmacy", "medical", "patient", "hipaa"],
    "logistics": ["logistics", "warehouse", "shipping", "freight", "supply chain", "delivery", "distribution"],
}


def fetch_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        print(f"Scraping: {url} ... done")
        return resp.text
    except Exception as e:
        print(f"WARNING: Failed to fetch {url}: {e}")
        return None


def clean_soup(html):
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag is None or not hasattr(tag, "attrs") or tag.attrs is None:
            continue
        classes = " ".join(tag.get("class", []))
        id_ = tag.get("id", "")
        combined = (classes + " " + id_).lower()
        if any(k in combined for k in ["cookie", "banner", "modal", "popup", "overlay"]):
            tag.decompose()
    return soup


def get_sentences(text):
    sentences = re.split(r'(?<=[.!?])\s+', text)
    return [s.strip() for s in sentences if len(s.strip()) > 20]


def detect_industry(text):
    text_lower = text.lower()
    found = []
    for industry, keywords in INDUSTRY_KEYWORDS.items():
        if any(kw in text_lower for kw in keywords):
            found.append(industry)
    return found


def extract_core_value_props(all_text):
    keywords = ["two-way", "two way", "conversational", "business messaging",
                 "team inbox", "shared inbox", "business texting", "sms platform"]
    sentences = get_sentences(all_text)
    matches = []
    seen = set()
    for s in sentences:
        s_lower = s.lower()
        if any(kw in s_lower for kw in keywords):
            if s not in seen:
                matches.append(s)
                seen.add(s)
    return matches[:20]


def extract_integrations(all_text):
    integration_names = [
        "HubSpot", "Salesforce", "Zapier", "Slack", "Zendesk", "Shopify",
        "Microsoft Teams", "Google", "ServiceNow", "Zoho", "Intercom",
        "Freshdesk", "ActiveCampaign", "Pipedrive", "Marketo", "Greenhouse",
        "BambooHR", "Workday", "ADP", "Oracle", "SAP"
    ]
    found = []
    flagged = []
    for name in integration_names:
        if re.search(re.escape(name), all_text, re.IGNORECASE):
            if name.lower() in ["hubspot", "salesforce"]:
                flagged.append(f"**{name}** (explicitly flagged)")
            else:
                found.append(name)
    return flagged, found


def extract_customer_stories(pages):
    stories = []
    for url, text in pages.items():
        if "customers" not in url and "blog" not in url:
            continue
        soup_text = text
        paragraphs = [p.strip() for p in re.split(r'\n{2,}', soup_text) if len(p.strip()) > 50]
        for para in paragraphs:
            industries = detect_industry(para)
            if industries or any(w in para.lower() for w in ["customer", "client", "company", "team", "business"]):
                tag = f"[industry: {', '.join(industries)}]" if industries else ""
                stories.append(f"- {para} {tag}".strip())
                if len(stories) >= 20:
                    break
    return stories


def extract_vertical_mentions(all_text):
    sentences = get_sentences(all_text)
    matches = []
    seen = set()
    all_kws = [kw for kws in INDUSTRY_KEYWORDS.values() for kw in kws]
    for s in sentences:
        s_lower = s.lower()
        if any(kw in s_lower for kw in all_kws):
            if s not in seen:
                industries = detect_industry(s)
                tag = f"[{', '.join(industries)}]" if industries else ""
                matches.append(f"- {s} {tag}".strip())
                seen.add(s)
    return matches[:20]


def extract_differentiators(all_text):
    keywords = ["one-way", "one way", "broadcast", "bulk sms", "blast",
                 "two-way", "conversational", "personal", "personalized", "reply"]
    sentences = get_sentences(all_text)
    matches = []
    seen = set()
    for s in sentences:
        s_lower = s.lower()
        if any(kw in s_lower for kw in keywords):
            if s not in seen:
                matches.append(f"- {s}")
                seen.add(s)
    return matches[:15]


def extract_blog_topics(blog_html):
    if not blog_html:
        return []
    soup = clean_soup(blog_html)
    topics = []
    # Try common blog post title patterns
    for tag in soup.find_all(["h2", "h3", "h4"]):
        title = tag.get_text(strip=True)
        if len(title) < 10 or len(title) > 200:
            continue
        # Try to get a summary from sibling/parent text
        summary = ""
        parent = tag.find_parent()
        if parent:
            p_tags = parent.find_all("p")
            if p_tags:
                summary = p_tags[0].get_text(strip=True)[:120]
        if title:
            line = f"- **{title}**"
            if summary:
                line += f" — {summary}{'...' if len(summary) == 120 else ''}"
            topics.append(line)
        if len(topics) >= 20:
            break
    return topics


def main():
    os.makedirs("shared", exist_ok=True)

    pages = {}
    blog_html = None
    all_text_parts = []

    for url in URLS:
        html = fetch_page(url)
        if html:
            soup = clean_soup(html)
            text = soup.get_text(separator=" ", strip=True)
            pages[url] = text
            all_text_parts.append(text)
            if url.endswith("/blog"):
                blog_html = html

    all_text = " ".join(all_text_parts)

    # Extract sections
    value_props = extract_core_value_props(all_text)
    flagged_integrations, other_integrations = extract_integrations(all_text)
    customer_stories = extract_customer_stories(pages)
    vertical_mentions = extract_vertical_mentions(all_text)
    differentiators = extract_differentiators(all_text)
    blog_topics = extract_blog_topics(blog_html)

    # Build markdown
    lines = ["# Heymarket Product Knowledge\n"]

    lines.append("## Core value props\n")
    if value_props:
        for s in value_props:
            lines.append(f"- {s}")
    else:
        lines.append("_No matches found._")
    lines.append("")

    lines.append("## Integrations\n")
    if flagged_integrations:
        lines.append("### Key CRM integrations")
        for item in flagged_integrations:
            lines.append(f"- {item}")
        lines.append("")
    if other_integrations:
        lines.append("### Other integrations mentioned")
        for name in other_integrations:
            lines.append(f"- {name}")
    if not flagged_integrations and not other_integrations:
        lines.append("_No integrations found._")
    lines.append("")

    lines.append("## Customer stories\n")
    if customer_stories:
        for story in customer_stories:
            lines.append(story)
    else:
        lines.append("_No customer stories found._")
    lines.append("")

    lines.append("## Vertical mentions\n")
    if vertical_mentions:
        for mention in vertical_mentions:
            lines.append(mention)
    else:
        lines.append("_No vertical mentions found._")
    lines.append("")

    lines.append("## Differentiators vs competitors\n")
    lines.append("_Theme: two-way conversational messaging vs one-way broadcast SMS_\n")
    if differentiators:
        for d in differentiators:
            lines.append(d)
    else:
        lines.append("_No differentiator language found._")
    lines.append("")

    lines.append("## Blog topics\n")
    if blog_topics:
        for topic in blog_topics:
            lines.append(topic)
    else:
        lines.append("_No blog topics found._")
    lines.append("")

    output = "\n".join(lines)
    output_path = "shared/product_knowledge.md"
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(output)

    word_count = len(output.split())
    print(f"\nWord count of {output_path}: {word_count}")
    print("Scrape complete. Review shared/product_knowledge.md before running optimize.py")


if __name__ == "__main__":
    main()
