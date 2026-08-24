'use strict';

const fs = require('fs');
const os = require('os');
const path = require('path');

const playwrightModule = process.env.PLAYWRIGHT_MODULE || 'playwright';
let chromium;
try {
  ({ chromium } = require(playwrightModule));
} catch (error) {
  console.error(
    `Unable to load Playwright from ${playwrightModule}. ` +
      'Install it locally or set PLAYWRIGHT_MODULE to an absolute package path.',
  );
  process.exit(1);
}

const baseUrl = process.env.DSH_URL || 'http://127.0.0.1:3081';
const outputDir = process.env.LIVE_INTERVIEW_OUT || path.join(os.tmpdir(), 'ouroboros-live-interview');
const prompt =
  process.argv[2] || 'ooo interview deepseek 하네스를 이용해서 마케팅 에이전트를 만들고싶어';

fs.mkdirSync(outputDir, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const context = await browser.newContext({ viewport: { width: 1400, height: 900 } });
  const page = await context.newPage();
  try {
    await page.goto(baseUrl, { waitUntil: 'domcontentloaded' });
    await page.waitForTimeout(3000);
    for (const name of ['Continue', 'Get started', 'Start']) {
      const button = page.getByRole('button', { name }).first();
      if (await button.isVisible().catch(() => false)) {
        await button.click().catch(() => {});
        break;
      }
    }
    await page.waitForTimeout(1500);

    const composer = page
      .locator('textarea[placeholder="Describe what you want to build"]')
      .first();
    await composer.waitFor({ state: 'visible', timeout: 30000 });
    await composer.fill(prompt);
    await page.keyboard.press('Enter');
    console.log('SENT:', prompt);

    const approver = setInterval(async () => {
      for (const name of ['Allow once', 'Allow', 'Approve', 'Yes']) {
        const button = page.getByRole('button', { name }).first();
        if (await button.isVisible().catch(() => false)) {
          await button.click().catch(() => {});
        }
      }
    }, 3000);

    await page
      .waitForFunction(() => /ouroboros_interview|ambiguity:/i.test(document.body.innerText), null, {
        timeout: 240000,
      })
      .catch(() => console.log('WARN: no interview tool call within 240s'));

    await page.waitForTimeout(45000);
    clearInterval(approver);

    const text = await page.evaluate(() => document.body.innerText);
    fs.writeFileSync(path.join(outputDir, 'live-interview.txt'), text);
    await page.screenshot({
      path: path.join(outputDir, 'live-interview.png'),
      fullPage: true,
    });

    console.log('TOOL CALLED:', /mcp__ouroboros__ouroboros_interview/.test(text));
    console.log('AMBIGUITY SHOWN:', /ambiguity:\s*[0-9.]+/i.test(text));
    console.log('OUTPUT:', outputDir);
    console.log('--- TAIL ---');
    console.log(text.slice(-1800));
  } catch (error) {
    await page
      .screenshot({ path: path.join(outputDir, 'live-interview-err.png') })
      .catch(() => {});
    console.error('FAIL:', String(error.message || error).split('\n')[0]);
    process.exitCode = 1;
  } finally {
    await context.close();
    await browser.close();
  }
})();
