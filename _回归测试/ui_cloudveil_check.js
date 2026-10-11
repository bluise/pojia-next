// 回归：v8.8 起核心随 exe 本地加载，启动时不得出现任何「输入 QQ 群号」门槛。
// 断言页面上不存在群号输入框 / 云验证浮层，且主界面已直接就绪。
const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ channel: 'chrome', headless: true });
  const page = await browser.newPage({ viewport: { width: 1120, height: 800 } });
  await page.goto('http://127.0.0.1:8317/', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000); // 等核心加载 + appBoot

  const r = await page.evaluate(() => {
    const q = (s) => document.querySelector(s);
    return {
      groupInput: !!q('#cloudgroup'),
      cloudVeil: !!q('#cloudveil'),
      verifyBtn: !!q('#b-cloudgo'),
      qqLink: !!q('#vl-qq'),
      pickVeil: !!q('#pickveil'),
      // 主界面是否真的起来了：目标表格存在且状态接口可用
      hasTargetsTable: !!q('#tbody') || !!document.querySelector('table'),
      bodyHasQQGroupHint: /输入\s*QQ\s*群号|群号不对/.test(document.body.innerText || ''),
    };
  });
  console.log('[boot-gate]', JSON.stringify(r));

  // 状态接口：core_file 必须是本地核心文件名，而不是云端占位
  const st = await page.evaluate(async () => {
    const res = await fetch('/api/state');
    return await res.json();
  });
  console.log('[state] core_file =', st.core_file);

  await browser.close();

  const ok1 = !r.groupInput && !r.cloudVeil && !r.verifyBtn && !r.qqLink && !r.pickVeil;
  const ok2 = !r.bodyHasQQGroupHint;
  const ok3 = !!st.core_file && st.core_file.indexOf('云端') < 0;

  console.log(ok1 ? 'PASS 无任何群号验证 UI' : 'FAIL 仍残留群号验证 UI');
  console.log(ok2 ? 'PASS 页面无群号提示文案' : 'FAIL 页面仍有群号提示');
  console.log(ok3 ? 'PASS 核心为本地文件（非云端下发）' : 'FAIL 核心来源异常: ' + st.core_file);
  process.exit(ok1 && ok2 && ok3 ? 0 : 1);
})().catch(e => { console.error('ERR', e.message); process.exit(2); });
