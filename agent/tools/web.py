import re
import urllib.request
import urllib.parse
import time
from .registry import tool

try:
    from selenium import webdriver
    from selenium.webdriver.chrome.service import Service as ChromeService
    from webdriver_manager.chrome import ChromeDriverManager
    from selenium.webdriver.chrome.options import Options
    from selenium.webdriver.common.by import By
    HAS_SELENIUM = True
except ImportError:
    HAS_SELENIUM = False


@tool("web_fetch", "Fetch a URL and return readable text (HTML stripped).", {"url": {"type": "string"}}, ["url"])
def web_fetch(cfg, url):
    if HAS_SELENIUM:
        options = Options()
        options.add_argument("--headless")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")
        
        driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
        try:
            driver.get(url)
            time.sleep(2)  # Wait for JS
            text = driver.find_element(By.TAG_NAME, "body").text
            return text.strip()
        except Exception as e:
            # Fallback to urllib if selenium fails
            pass
        finally:
            driver.quit()

    req = urllib.request.Request(url, headers={"User-Agent": "omni-agent/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read(2_000_000).decode(errors="replace")
    raw = re.sub(r"(?is)<(script|style).*?</\1>", " ", raw)
    text = re.sub(r"(?s)<[^>]+>", " ", raw)
    return re.sub(r"\s+", " ", text).strip()


@tool("web_search", "Search the web using Google.", {"query": {"type": "string"}}, ["query"])
def web_search(cfg, query):
    if not HAS_SELENIUM:
        return "Error: selenium and webdriver-manager are not installed. Please install them to use web_search."
    
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36")
    
    driver = webdriver.Chrome(service=ChromeService(ChromeDriverManager().install()), options=options)
    try:
        driver.get(f"https://www.google.com/search?q={urllib.parse.quote(query)}")
        time.sleep(2)
        results = []
        # Google search results are usually in div.g or similar
        for g in driver.find_elements(By.CSS_SELECTOR, "div.g"):
            try:
                anchors = g.find_elements(By.TAG_NAME, "a")
                if not anchors: continue
                link = anchors[0].get_attribute("href")
                title_el = g.find_elements(By.TAG_NAME, "h3")
                title = title_el[0].text if title_el else "No title"
                
                # Snippet is often in a div with a specific class or just text
                snippet = ""
                # Try common snippet classes
                for cls in ["VwiC3b", "yXM94b", "MUwY0b"]:
                    s_el = g.find_elements(By.CLASS_NAME, cls)
                    if s_el:
                        snippet = s_el[0].text
                        break
                
                if link and title:
                    results.append(f"Title: {title}\nLink: {link}\nSnippet: {snippet}\n")
            except:
                continue
        
        if not results:
            # Try a simpler approach if the above fails (Google changes layout often)
            body_text = driver.find_element(By.TAG_NAME, "body").text
            return "Search results (raw text):\n" + body_text[:2000]

        return "\n".join(results[:10])
    except Exception as e:
        return f"Error during search: {str(e)}"
    finally:
        driver.quit()
