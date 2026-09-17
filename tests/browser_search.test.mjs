import { readFile } from 'node:fs/promises';
import assert from 'node:assert/strict';
import { test } from 'node:test';

const code = await readFile(new URL('../src/chrome_extension/browser_search.js', import.meta.url), 'utf8');
const { withPage, cmdBrowserSearch, cmdBrowserRead } = await import(`data:text/javascript;base64,${Buffer.from(code).toString('base64')}`);

test('temporary tab is inactive and cleaned after injection failure', async () => {
  const removed = [];
  globalThis.chrome = {
    tabs: { create: async options => { assert.equal(options.active, false); return {id: 42}; },
      get: async () => ({status: 'complete'}), remove: async id => removed.push(id) },
    scripting: { executeScript: async () => { throw new Error('blocked'); } },
  };
  await assert.rejects(withPage('https://example.org', () => {}, []), /blocked/);
  assert.deepEqual(removed, [42]);
});

test('search extracts organic results, deduplicates, decodes Bing URLs', async () => {
  const target = 'https://example.org/article';
  const cards = [target, target].map(url => ({ querySelector: selector => selector === 'h2 a'
    ? {href: `https://www.bing.com/ck/a?u=a1${Buffer.from(url).toString('base64url')}`, innerText: 'Title'}
    : {innerText: 'Snippet'} }));
  globalThis.document = { querySelectorAll: () => cards };
  globalThis.location = {href: 'https://www.bing.com/search?q=test'};
  globalThis.chrome = {
    tabs: {create: async () => ({id: 43}), get: async () => ({status: 'complete'}), remove: async () => {}},
    scripting: {executeScript: async ({func, args}) => [{result: await func(...args)}]},
  };
  const result = await cmdBrowserSearch({query: 'test'});
  assert.equal(result.results.length, 1);
  assert.equal(result.results[0].url, target);
});

test('read prefers article-like content and can return bounded rendered HTML', async () => {
  const article = {
    innerText: '陕西省传统医学师承政策正文。'.repeat(20),
    querySelectorAll: selector => selector === 'a' ? [] : [{}, {}, {}],
  };
  const body = {
    innerText: '首页 导航 ' + article.innerText + ' 推荐阅读 联系我们',
    querySelectorAll: selector => selector === 'a' ? [{innerText: '首页 导航 推荐阅读 联系我们'}] : [],
  };
  globalThis.location = {href: 'https://example.org/policy'};
  globalThis.document = {
    body,
    documentElement: {lang: 'zh-CN', outerHTML: '<html><body>政策正文</body></html>'},
    title: '政策通知',
    querySelectorAll: selector => selector === 'article' ? [article] : [],
    querySelector: selector => {
      if (selector === 'link[rel="canonical"]') return {href: 'https://example.org/policy'};
      if (selector === 'time[datetime]') return {getAttribute: () => '2026-09-17'};
      return null;
    },
  };
  globalThis.chrome = {
    tabs: {create: async () => ({id: 44}), get: async () => ({status: 'complete'}), remove: async () => {}},
    scripting: {executeScript: async ({func, args}) => [{result: await func(...args)}]},
  };
  const result = await cmdBrowserRead({url: 'https://example.org/policy', maxChars: 2000, includeHtml: true, maxHtmlChars: 20});
  assert.match(result.extraction_method, /^heuristic:/);
  assert.ok(result.text.startsWith('陕西省传统医学师承政策正文'));
  assert.equal(result.published_at, '2026-09-17');
  assert.equal(result.lang, 'zh-CN');
  assert.equal(result.html.length, 20);
  assert.equal(result.html_truncated, true);
});

test('read discovers CMS content through a title-like ancestor', async () => {
  const content = {
    innerText: '政府政策正文。'.repeat(80),
    className: 'news-content',
    parentElement: null,
    querySelectorAll: selector => selector === 'a' ? [{innerText: '附件'}] : [{}, {}, {}, {}],
  };
  const heading = {
    innerText: '关于开展2026年传统医学师承出师考核报名工作的通知',
    className: 'news-title',
    parentElement: content,
  };
  const body = {
    innerText: '首页 导航 联系我们 ' + content.innerText + ' 页脚 网站地图',
    querySelectorAll: selector => selector === 'a'
      ? Array.from({length: 20}, () => ({innerText: '导航链接'}))
      : [],
  };
  globalThis.location = {href: 'https://example.org/cms'};
  globalThis.document = {
    body,
    documentElement: {lang: 'zh-CN', outerHTML: '<html></html>'},
    title: '某政府网站_关于开展2026年传统医学师承出师考核报名工作的通知',
    querySelectorAll: selector => {
      if (selector === "h1,h2,[class*='title'],[id*='title']") return [heading];
      return [];
    },
    querySelector: selector => selector === 'link[rel="canonical"]' ? {href: 'https://example.org/cms'} : null,
  };
  globalThis.chrome = {
    tabs: {create: async () => ({id: 46}), get: async () => ({status: 'complete'}), remove: async () => {}},
    scripting: {executeScript: async ({func, args}) => [{result: await func(...args)}]},
  };
  const result = await cmdBrowserRead({url: 'https://example.org/cms', maxChars: 5000});
  assert.equal(result.extraction_method, 'heuristic:heading-ancestor-1');
  assert.ok(result.text.startsWith('政府政策正文'));
});

test('read emits source hash and attachment candidates from chosen content', async () => {
  const attachment = {
    href: 'https://example.org/files/policy.docx',
    innerText: '附件1 政策材料',
    textContent: '附件1 政策材料',
    getAttribute: name => name === 'download' ? '' : null,
  };
  const article = {
    innerText: '政策正文。'.repeat(50),
    querySelectorAll: selector => selector === 'a[href]' ? [attachment] : selector === 'a' ? [attachment] : [{}, {}, {}],
  };
  const body = {innerText: article.innerText, querySelectorAll: () => []};
  globalThis.location = {href: 'https://example.org/policy'};
  globalThis.document = {
    body,
    documentElement: {lang: 'zh-CN', outerHTML: '<html><body>source snapshot</body></html>'},
    title: '政策通知',
    querySelectorAll: selector => selector === 'article' ? [article] : [],
    querySelector: selector => selector === 'link[rel="canonical"]' ? {href: 'https://example.org/policy'} : null,
  };
  globalThis.chrome = {
    tabs: {create: async () => ({id: 47}), get: async () => ({status: 'complete'}), remove: async () => {}},
    scripting: {executeScript: async ({func, args}) => [{result: await func(...args)}]},
  };
  const result = await cmdBrowserRead({url: 'https://example.org/policy', maxChars: 5000});
  assert.equal(result.extractor_version, 'browser_read_v3');
  assert.equal(result.source_hash.length, 64);
  assert.equal(result.source_html_length, '<html><body>source snapshot</body></html>'.length);
  assert.equal(result.attachments.length, 1);
  assert.equal(result.attachments[0].url, 'https://example.org/files/policy.docx');
});

test('read falls back to visible publication date text when metadata is absent', async () => {
  const article = {
    innerText: '发布日期：2026-03-13 17:38\n' + '政府通知正文。'.repeat(30),
    querySelectorAll: selector => selector === 'a' ? [] : [{}, {}, {}],
  };
  const body = {
    innerText: '导航\n' + article.innerText + '\n页脚',
    querySelectorAll: () => [],
  };
  globalThis.location = {href: 'https://example.org/gov'};
  globalThis.document = {
    body,
    documentElement: {lang: 'zh-CN', outerHTML: '<html></html>'},
    title: '政府通知',
    querySelectorAll: selector => selector === 'article' ? [article] : [],
    querySelector: selector => selector === 'link[rel="canonical"]' ? {href: 'https://example.org/gov'} : null,
  };
  globalThis.chrome = {
    tabs: {create: async () => ({id: 45}), get: async () => ({status: 'complete'}), remove: async () => {}},
    scripting: {executeScript: async ({func, args}) => [{result: await func(...args)}]},
  };
  const result = await cmdBrowserRead({url: 'https://example.org/gov', maxChars: 2000});
  assert.equal(result.published_at, '2026-03-13 17:38');
});
