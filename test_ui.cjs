// Optional browser regression check: node test_ui.cjs /path/to/playwright
const { chromium } = require(process.argv[2] || 'playwright');
const fs = require('node:fs');
const assert = require('node:assert/strict');

(async () => {
  const browser = await chromium.launch({ headless: true, executablePath: process.argv[3] });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 }, reducedMotion: 'reduce' });
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    const row = (key, fields = {}) => ({ key, sessionId: key, provider: 'claude',
      parentKey: null, state: 'resting', name: key, title: key, cwd: '/project',
      startedAt: 1, state_since: 1, foot: 'resting', hasTab: false, ...fields });
    const snapshot = { sessions: [
      row('parent'), row('child', { parentKey: 'parent', kind: 'subagent', state: 'needs-you', title: 'Needle child' }),
      row('codex', { provider: 'codex', state: 'working', title: '<script>unsafe</script>' }),
    ], meta: { now: 1000, counts: { resting: 1, 'needs-you': 1, working: 1 }, hooks_installed: true, hook_events: 3 } };
    await page.addInitScript(() => {
      window.EventSource = class {
        constructor() { window.testSource = this; }
        addEventListener(name, fn) { this[name] = fn; }
      };
    });
    await page.route('**/*', route => {
      const path = new URL(route.request().url()).pathname;
      if (path === '/') return route.fulfill({ contentType: 'text/html', body: fs.readFileSync(__dirname + '/static/index.html', 'utf8') });
      if (path === '/api/snapshot') return route.fulfill({ contentType: 'application/json', body: JSON.stringify(snapshot) });
      return route.abort();
    });
    await page.goto('http://session-board.test/');
    await page.waitForSelector('[data-key="child"]');
    assert.equal(await page.locator('#lanterns > .session-card').count(), 2);
    assert.equal(await page.locator('[data-key="parent"] > .agent-group > .children > [data-key="child"]').count(), 1);
    assert.equal(await page.locator('[data-key="parent"] > .agent-group').evaluate(el => el.open), true);
    await page.locator('[data-key="parent"] > .agent-group > summary').click();
    assert.equal(await page.locator('[data-key="child"]').isVisible(), false);
    await page.locator('#search').fill('needle');
    assert.equal(await page.locator('[data-key="child"]').isVisible(), true);
    assert.equal(await page.locator('.session-card').count(), 2);
    assert.equal(await page.locator('#visibleCount').textContent(), '1 of 3');
    await page.locator('#search').fill('');
    await page.locator('#providerFilter').selectOption('codex');
    assert.equal(await page.locator('.session-card').count(), 1);
    await page.locator('.inspect').click();
    assert.equal(await page.locator('dialog').evaluate(el => el.open), true);
    assert.equal(await page.locator('#dialogTitle').textContent(), '<script>unsafe</script>');
    assert.equal(await page.locator('#raiseTab').isVisible(), false);
    await page.keyboard.press('Escape');
    await page.locator('#compact').check();
    await page.reload();
    await page.waitForSelector('[data-key="codex"]');
    assert.equal(await page.locator('#compact').isChecked(), true);
    assert.equal(await page.locator('#providerFilter').inputValue(), 'codex');
    await page.evaluate(() => window.testSource.onerror());
    assert.match(await page.locator('#status').textContent(), /lost the server/);
    await page.locator('#search').fill('no match');
    assert.equal(await page.locator('#empty').isVisible(), true);
    assert.match(await page.locator('#status').textContent(), /lost the server/);
    await page.locator('#search').fill('');
    await page.locator('#providerFilter').selectOption('');
    await page.evaluate(snap => window.testSource.snapshot({ data: JSON.stringify(snap) }), snapshot);
    assert.doesNotMatch(await page.locator('#status').textContent(), /lost the server/);
    await page.setViewportSize({ width: 390, height: 844 });
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    await page.screenshot({ path: '/tmp/session-board-mobile.png', fullPage: true });
    await page.setViewportSize({ width: 1280, height: 900 });
    await page.locator('#compact').uncheck();
    await page.screenshot({ path: '/tmp/session-board-desktop.png', fullPage: true });
    assert.deepEqual(errors, []);
    console.log('Browser checks passed: hierarchy, search, providers, details, escaping, persistence, disconnect/reconnect, mobile layout.');
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
