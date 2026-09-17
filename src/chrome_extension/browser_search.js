// Only tabs created by these operations are navigated and closed.
export async function withPage(url, func, args) {
  const parsed = new URL(url);
  if (!["https:", "http:"].includes(parsed.protocol) || parsed.username || parsed.password) {
    throw new Error("Expected an HTTP(S) URL without credentials");
  }
  const tab = await chrome.tabs.create({ url: parsed.href, active: false });
  try {
    const deadline = Date.now() + 20000;
    while ((await chrome.tabs.get(tab.id)).status !== "complete") {
      if (Date.now() >= deadline) throw new Error("Browser page load timed out");
      await new Promise(resolve => setTimeout(resolve, 200));
    }
    const [injection] = await chrome.scripting.executeScript({ target: { tabId: tab.id }, func, args });
    if (!injection?.result) throw new Error("Page extraction returned no result");
    return injection.result;
  } finally {
    await chrome.tabs.remove(tab.id).catch(() => {});
  }
}

export async function cmdBrowserSearch({ query, count = 5 }) {
  if (typeof query !== "string" || !query.trim()) throw new Error("Query cannot be empty");
  count = Math.max(1, Math.min(10, Number(count) || 5));
  return withPage(`https://www.bing.com/search?q=${encodeURIComponent(query)}`, (limit) => {
    const results = [];
    const seen = new Set();
    for (const card of document.querySelectorAll("#b_results .b_algo")) {
      const anchor = card.querySelector("h2 a");
      if (!anchor) continue;
      let url = anchor.href;
      try {
        const parsed = new URL(url);
        const encoded = parsed.searchParams.get("u");
        if ((parsed.hostname === "bing.com" || parsed.hostname.endsWith(".bing.com")) && parsed.pathname === "/ck/a" && encoded?.startsWith("a1")) {
          url = new TextDecoder().decode(Uint8Array.from(atob(encoded.slice(2).replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0)));
        }
        if (!["http:", "https:"].includes(new URL(url).protocol)) continue;
      } catch { continue; }
      if (seen.has(url)) continue;
      seen.add(url);
      results.push({ title: anchor.innerText.slice(0, 500), url,
        snippet: (card.querySelector(".b_caption p")?.innerText || card.innerText).slice(0, 2000) });
      if (results.length >= limit) break;
    }
    return { results, search_url: location.href,
      status: results.length ? "ok" : "no_results_or_blocked",
      message: results.length ? "" : "No results extracted: page may require consent/CAPTCHA or its layout has changed." };
  }, [count]);
}

export async function cmdBrowserRead({ url, maxChars = 30000, includeHtml = false, maxHtmlChars = 0 }) {
  const options = {
    textLimit: Math.max(1000, Math.min(2000000, Number(maxChars) || 30000)),
    includeHtml: Boolean(includeHtml),
    htmlLimit: Math.max(0, Math.min(4000000, Number(maxHtmlChars) || 0)),
  };
  return withPage(url, async (opts) => {
    // Some mainland government/university pages populate the article shortly after
    // load. Wait for meaningful body text, but keep the wait bounded.
    for (let i = 0; i < 20 && (document.body?.innerText || "").trim().length < 80; i++) {
      await new Promise(resolve => setTimeout(resolve, 200));
    }

    const selectors = [
      "article", "main", "[role='main']", ".article-content", ".article_content",
      ".article", ".TRS_Editor", "#zoom", "#content", ".rich_media_content",
      "#js_content", ".post-content", ".entry-content", ".content",
    ];
    const candidates = [];
    const seen = new Set();
    const addCandidate = (element, selector, depthBonus = 0) => {
      if (!element || seen.has(element)) return;
      seen.add(element);
      const text = (element.innerText || "").trim();
      if (text.length < 120) return;
      const links = Array.from(element.querySelectorAll("a"));
      const linkText = links.reduce((sum, anchor) => sum + (anchor.innerText || "").trim().length, 0);
      const paragraphs = element.querySelectorAll("p,li,br").length;
      const linkPenalty = linkText * 1.8 + links.length * 28;
      const score = text.length - linkPenalty + Math.min(paragraphs, 100) * 24 + depthBonus;
      candidates.push({ element, selector, text, score });
    };
    for (const selector of selectors) {
      for (const element of document.querySelectorAll(selector)) addCandidate(element, selector, 80);
    }
    // Government and university CMS templates often use opaque container class
    // names. A title-like node that agrees with document.title is a reliable anchor:
    // inspect a bounded ancestor chain and prefer the smallest text-rich,
    // low-link-density parent around it.
    const normalizedDocumentTitle = (document.title || "").replace(/\s+/g, "").toLowerCase();
    for (const heading of document.querySelectorAll("h1,h2,[class*='title'],[id*='title']")) {
      const headingText = (heading.innerText || "").trim();
      if (headingText.length < 8 || headingText.length > 240) continue;
      const normalizedHeading = headingText.replace(/\s+/g, "").toLowerCase();
      if (normalizedDocumentTitle && !normalizedDocumentTitle.includes(normalizedHeading)
          && !normalizedHeading.includes(normalizedDocumentTitle)) continue;
      let node = heading.parentElement;
      for (let depth = 1; node && depth <= 6 && node !== document.body; depth++, node = node.parentElement) {
        addCandidate(node, `heading-ancestor-${depth}`, Math.max(0, 180 - depth * 24));
      }
    }
    if (document.body) {
      const bodyText = (document.body.innerText || "").trim();
      const bodyLinkText = Array.from(document.body.querySelectorAll("a"))
        .reduce((sum, anchor) => sum + (anchor.innerText || "").trim().length, 0);
      candidates.push({ element: document.body, selector: "body", text: bodyText,
        score: bodyText.length - bodyLinkText * 1.6 });
    }
    candidates.sort((a, b) => b.score - a.score);
    const chosen = candidates[0] || { selector: "body", text: document.body?.innerText || "" };
    const text = chosen.text || "";

    const metaContent = (...keys) => {
      for (const key of keys) {
        const node = document.querySelector(`meta[name="${key}"],meta[property="${key}"]`);
        const value = node?.getAttribute("content")?.trim();
        if (value) return value;
      }
      return "";
    };
    const canonical = document.querySelector('link[rel="canonical"]')?.href || location.href;
    let publishedAt = metaContent("article:published_time", "date", "pubdate", "publishdate", "publish-time")
      || document.querySelector("time[datetime]")?.getAttribute("datetime") || "";
    if (!publishedAt) {
      const dateMatch = (text || document.body?.innerText || "").match(
        /(?:发布日期|发布时间|发布(?:日期|时间)?|日期)\s*[：:]?\s*((?:19|20)\d{2}[年\-\/.]\d{1,2}[月\-\/.]\d{1,2}日?(?:\s+\d{1,2}:\d{2})?)/
      );
      if (dateMatch) {
        publishedAt = dateMatch[1]
          .replace(/年|\//g, "-")
          .replace(/月/g, "-")
          .replace(/日/g, "");
      }
    }

    const sourceHtml = document.documentElement?.outerHTML || "";
    const attachmentExtensions = /\.(?:pdf|docx?|xlsx?|pptx?|csv|txt|zip|rar|7z)(?:$|[?#])/i;
    const attachments = [];
    const attachmentSeen = new Set();
    for (const anchor of Array.from(chosen.element?.querySelectorAll?.("a[href]") || [])) {
      const href = anchor.href || anchor.getAttribute?.("href") || "";
      if (!href || attachmentSeen.has(href)) continue;
      const label = (anchor.innerText || anchor.textContent || "").trim();
      const downloadName = (anchor.getAttribute?.("download") || "").trim();
      if (!downloadName && !attachmentExtensions.test(href) && !/(附件|下载|download)/i.test(label)) continue;
      try {
        const parsedAttachment = new URL(href, location.href);
        if (!["http:", "https:"].includes(parsedAttachment.protocol)) continue;
        attachmentSeen.add(parsedAttachment.href);
        attachments.push({
          url: parsedAttachment.href,
          name: downloadName || label.slice(0, 300),
          link_text: label.slice(0, 500),
        });
        if (attachments.length >= 50) break;
      } catch {}
    }

    // Raw rendered DOM contains volatile script nonces and runtime attributes on many
    // sites, so it is evidence but not a stable source identity. Build the snapshot
    // fingerprint from normalized visible page text plus canonical metadata and
    // attachment URLs. This stays independent of the selected extraction container.
    const normalizedBodyText = (document.body?.innerText || "").replace(/\s+/g, " ").trim();
    const sourceMaterial = [
      canonical,
      document.title || "",
      normalizedBodyText,
      ...attachments.map(item => item.url).sort(),
    ].join("\n");
    let sourceHash = "";
    if (globalThis.crypto?.subtle && sourceMaterial) {
      const digest = await globalThis.crypto.subtle.digest("SHA-256", new TextEncoder().encode(sourceMaterial));
      sourceHash = Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, "0")).join("");
    }

    const result = {
      url: location.href,
      canonical_url: canonical,
      title: document.title,
      author: metaContent("author", "article:author"),
      published_at: publishedAt,
      description: metaContent("description", "og:description"),
      lang: document.documentElement?.lang || "",
      extraction_method: chosen.selector === "body" ? "body_fallback" : `heuristic:${chosen.selector}`,
      extractor_version: "browser_read_v3",
      source_hash: sourceHash,
      source_hash_kind: "visible_body_text_v1",
      source_html_length: sourceHtml.length,
      attachments,
      text: text.slice(0, opts.textLimit),
      truncated: text.length > opts.textLimit,
      original_length: text.length,
    };
    if (opts.includeHtml) {
      const html = document.documentElement?.outerHTML || "";
      const htmlLimit = opts.htmlLimit || html.length;
      result.html = html.slice(0, htmlLimit);
      result.html_truncated = html.length > htmlLimit;
      result.html_original_length = html.length;
    }
    return result;
  }, [options]);
}
