import os
import json
import sys
import requests
import urllib.parse
import smtplib
import gspread
from bs4 import BeautifulSoup
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from google.oauth2.service_account import Credentials
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from typing import List
from playwright.sync_api import sync_playwright
import re
import urllib.parse


# 1. DEFINE YOUR STRUCTURAL DATA MODELS
class CPDEvent(BaseModel):
    date: str = Field(description="The event date, format YYYY-MM-DD or specific text description.")
    source: str = Field(description="The company, hospital, university, or platform hosting the training.")
    description: str = Field(description="The primary title or topic name of the workshop/webinar/ebook.")
    presented_by: str = Field(description="The specific instructor, panel, speaker, or clinical expert hosting it.")
    what_you_will_learn: str = Field(description="A concise 1-sentence summary detailing what the attendee will learn.")
    # 🔥 UPDATED: Force Gemini to look for the deep path string/identifier instead of guessing a domain
    registration_link: str = Field(description="Extract any dynamic reference, event ID, slug, or relative path found near the course context (e.g., '/event/?Event_id=4364' or 'View Details link string').")

class CPDEventCollection(BaseModel):
    events: List[CPDEvent] = Field(
        description="A complete list of ALL upcoming physiotherapy CPD events, workshops, webinars, or ebooks found on the page.")


# 2. RUN OPEN-WEB DISCOVERY PORT (No API Keys Needed)
def discover_nz_physio_links():
    """Scrapes DuckDuckGo HTML layout to find new NZ Physio CPD opportunities across the open web."""
    print("🔍 Fetching new links from the open web via DuckDuckGo...")

    query = '"physiotherapy" CPD workshop New Zealand site:nz'
    encoded_query = urllib.parse.quote(query)
    url = f"https://html.duckduckgo.com/html/?q={encoded_query}"

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
    }

    found_urls = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        soup = BeautifulSoup(response.text, "html.parser")

        for anchor in soup.find_all("a", class_="result__url"):
            href = anchor.get("href", "")
            if "uddg=" in href:
                actual_url = href.split("uddg=")[1].split("&")[0]
                actual_url = urllib.parse.unquote(actual_url)
                found_urls.append(actual_url)
            elif href.startswith("http"):
                found_urls.append(href)

    except Exception as e:
        print(f"⚠️ Search discovery component warning: {e}")

    return list(set(found_urls))


# 3. TRANSFORMS MESSY TEXT INTO COLD STRUCTURED EXCEL ROWS
def extract_fields_with_ai(target_url: str, webpage_text: str):
    """Token-optimized extraction engine with automated local fallback handling."""
    try:
        # 1. Check if key exists in the environment first
        if not os.environ.get("GEMINI_API_KEY"):
            raise ValueError("GEMINI_API_KEY environment variable is unassigned.")

        client = genai.Client()
        optimized_text = webpage_text.strip()[:4000]

        prompt = f"""
        Identify and extract ALL upcoming New Zealand Physiotherapist CPDs, courses, or webinars.
        Webpage content:
        {optimized_text}
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CPDEventCollection,
                temperature=0.1
            ),
        )

        data = json.loads(response.text)
        extracted_events = data.get("events", [])

        processed_rows = []
        for event in extracted_events:
            extracted_link = event.get("registration_link", "").strip()

            # URL normalization
            if extracted_link and not extracted_link.startswith("http"):
                base_parsed = urllib.parse.urlparse(target_url)
                base_domain = f"{base_parsed.scheme}://{base_parsed.netloc}"
                if not extracted_link.startswith("/"):
                    extracted_link = "/" + extracted_link
                final_url = f"{base_domain}{extracted_link}"
            elif not extracted_link or extracted_link == target_url:
                sanitized_title = urllib.parse.quote(event.get("description", "event"))
                final_url = f"{target_url}#event-{sanitized_title}"
            else:
                final_url = extracted_link

            processed_rows.append([
                event.get("date", "Upcoming"),
                event.get("source", "Discovered Link"),
                event.get("description", "CPD Training Track"),
                event.get("presented_by", "N/A"),
                event.get("what_you_will_learn", "Click registration link to learn more"),
                final_url
            ])

        return processed_rows

    except Exception as api_error:
        # 🔥 FIXED: Instead of printing and returning [], explicitly run the fallback here
        print(f"\n⚠️ Gemini API Error (429/Quota/Network): {api_error}")
        print(f"🔄 Switching context over to the free local fallback engine for: {target_url}...\n")

        # Call the local regex parser function and return its extracted data rows directly
        fallback_rows = extract_fields_with_local_regex(target_url, webpage_text)
        return fallback_rows


def extract_fields_with_local_regex(target_url: str, webpage_text: str):
    """
    A 100% free, local fallback parsing engine that uses text patterns
    and regex to extract workshops when AI tokens are exhausted.
    """
    print(f"🛠️ Executing Local Regex Parsing Fallback for {target_url}...")
    processed_rows = []

    # Split text body into distinct single lines to isolate individual events
    lines = [line.strip() for line in webpage_text.split('\n') if len(line.strip()) > 20]

    # Regex pattern to match common NZ date distributions (e.g., 15 July 2026, 2026-08-12)
    date_pattern = r'(\b\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+2026\b|\b2026[-/]\d{2}[-/]\d{2}\b)'

    for line in lines:
        # Check if the line looks like a workshop or course advertisement
        if any(keyword in line.lower() for keyword in ["workshop", "cpd", "webinar", "course", "seminar", "training"]):
            # Find a date stamp within the local text layout line
            date_match = re.search(date_pattern, line, re.IGNORECASE)
            event_date = date_match.group(0) if date_match else "Upcoming 2026"

            # Clean up the line text to serve as our course description
            clean_desc = line[:120].strip()

            # Create a localized unique page hash parameter to prevent deduplication blocks
            sanitized_title = urllib.parse.quote(clean_desc[:30])
            final_url = f"{target_url}#event-{sanitized_title}"

            # Deduce a placeholder provider based on the root web address domain
            parsed_domain = urllib.parse.urlparse(target_url).netloc.replace("www.", "")

            processed_rows.append([
                event_date,
                parsed_domain,
                clean_desc,
                "See website for details",  # Presenter placeholder
                "Professional Clinical Development module",  # What you'll learn placeholder
                final_url
            ])

    # Limit row yields to prevent junk duplication if a page has generic sidebar text
    return processed_rows[:8]
# 4. SECURE ISOLATED EMAIL DELIVERY DIGEST (System-Generated SMTP)
def send_email_digest(new_leads):
    """Sends a bi-monthly digest to your inbox using a third-party transactional SMTP relay."""
    smtp_server = "smtp-relay.brevo.com"
    smtp_port = 587
    smtp_username = os.environ.get("BREVO_USERNAME")
    smtp_password = os.environ.get("BREVO_SMTP_KEY")
    recipient_email = os.environ.get("MY_PERSONAL_EMAIL")

    if not all([smtp_username, smtp_password, recipient_email]):
        print("⚠️ Email configuration missing from env variables. Skipping notification step.")
        return

    sender_email = "cpd-automation-bot@system.local"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = "🚨 Bi-Monthly Top 10 NZ Physio CPD Digest"
    msg["From"] = f"NZ Physio Bot <{sender_email}>"
    msg["To"] = recipient_email

    html_content = """
    <html>
      <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.5;">
        <h2 style="color: #007bc4;">🎯 Top 10 Latest Physiotherapy CPDs & Webinars</h2>
        <p>Here is your automated summary of new courses discovered on the open web:</p>
        <hr style="border: 0; border-top: 1px solid #ddd; margin: 20px 0;"/>
    """

    # Take only the top 10 newest leads for the summary digest array
    for idx, lead in enumerate(new_leads[:10], 1):
        html_content += f"""
        <div style="margin-bottom: 20px; padding: 15px; background-color: #f9f9f9; border-left: 4px solid #007bc4; border-radius: 4px;">
            <strong style="font-size: 16px; color: #111;">{idx}. {lead[2]}</strong> <span style="color: #777;">(via {lead[1]})</span><br/>
            <span style="color: #555; font-size: 13px;"><strong>Presenter:</strong> {lead[3]} | <strong>Date:</strong> {lead[0]}</span><br/>
            <p style="margin: 8px 0; color: #444;">💡 <em>What you'll learn:</em> {lead[4]}</p>
            <a href="{lead[5]}" style="color: #007bc4; text-decoration: none; font-weight: bold; font-size: 14px;">👉 View & Register Here</a>
        </div>
        """

    html_content += """
        <hr style="border: 0; border-top: 1px solid #ddd; margin: 20px 0;"/>
        <p style="font-size: 11px; color: #999;">This is an automated system run broadcast managed via GitHub Actions. No reply required.</p>
      </body>
    </html>
    """
    msg.attach(MIMEText(html_content, "html"))

    try:
        server = smtplib.SMTP(smtp_server, smtp_port)
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(sender_email, recipient_email, msg.as_string())
        server.quit()
        print("✉️ HTML summary digest email dispatched securely via Brevo.")
    except Exception as e:
        print(f"⚠️ Failed to send email via SMTP relay: {e}")


# 5. ORCHESTRATION PIPELINE MANAGER
def main():
    print("🚀 Initializing Live AI Discovery Pipeline Execution Loop...")

    try:
        SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds_dict = json.loads(os.environ["GOOGLE_CREDENTIALS"])
        creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
        client = gspread.authorize(creds)
        sheet = client.open("NZ_Physio_CPD_Tracker")
        leads_tab = sheet.worksheet("Leads")
        history_tab = sheet.worksheet("History")
    except Exception as e:
        print(f"❌ Google Workspace Link Error: {e}")
        sys.exit(1)

    existing_links = set(history_tab.col_values(1))
    discovered_urls = discover_nz_physio_links()
    print(f"🎯 Discovered {len(discovered_urls)} candidates across the open web.")

    new_leads = []
    new_history_entries = []

    print("🌐 Launching headless web driver backend...")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # Inside your main() function loop:
        for url in discovered_urls:
            if any(x in url for x in ["duckduckgo.com", "google.com", "wikipedia.org"]):
                continue

            if url in existing_links:
                print(f"Skip {url} (Already in Sheet database)")
                continue

            print(f"📖 Scraping text from node: {url}")
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(2000)

                body_text = page.inner_text("body")

                # 💡 TOKEN FIX: Local Pre-validation Gate
                # If the webpage text doesn't mention core terms, don't spend AI tokens processing it
                text_lower = body_text.lower()
                has_keywords = any(
                    k in text_lower for k in ["cpd", "workshop", "webinar", "course", "training", "seminar"])
                has_year = "2026" in text_lower

                if not (has_keywords and has_year):
                    print(f"   ⏭️ Skipping page locally (No relevant 2026 CPD context found). Saved tokens!")
                    continue

                print("🧠 Passing filtered text to Gemini for batch structured extraction...")
                row_payloads = extract_fields_with_ai(url, body_text)

                if row_payloads:
                    print(f"   ↳ 📋 Gemini found {len(row_payloads)} workshops on this page.")
                    for row in row_payloads:
                        item_link = row[5]
                        if item_link not in existing_links:
                            new_leads.append(row)
                            new_history_entries.append([item_link])
                            existing_links.add(item_link)
                        else:
                            print(f"   ⏭️ Skipping duplicate workshop link inside page: {item_link}")

            except Exception as page_err:
                print(f"⚠️ Skipping unreachable/blocked link [{url}]: {page_err}")
                continue

        browser.close()

    if new_leads:
        print(f"💾 Committing {len(new_leads)} unique discoveries directly into Google Sheets columns...")
        leads_tab.append_rows(new_leads)
        history_tab.append_rows(new_history_entries)
        print("✅ Sheet sync completed successfully!")

        # Dispatch the HTML formatting engine notification
        print("📨 Compiling and sending email digest summaries...")
        send_email_digest(new_leads)
    else:
        print("😴 No fresh untracked items identified during this cycle.")


if __name__ == "__main__":
    main()