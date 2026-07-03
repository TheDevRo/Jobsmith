import { initStagehand, fillFormDirectly, actWithRetry } from './lib/stagehand-client.js';
import { createLogger } from './lib/logger.js';

async function testFixes() {
  const { entries, log } = createLogger();
  const stagehand = await initStagehand({ headless: true });
  const page = stagehand.context.activePage();

  try {
    console.log('Page keys:', Object.keys(page));
    const playwrightPage = page.page || page._page;
    if (!playwrightPage) {
      // Just try page directly if it looks like a playwright page
      if (typeof page.goto === 'function' && typeof page.setContent === 'function') {
        console.log('Page object itself seems to be the playwright page');
      } else {
        throw new Error('Could not find underlying Playwright page on stagehand page object. Keys: ' + Object.keys(page).join(', '));
      }
    }

    console.log('--- Testing fillFormDirectly ---');
    await playwrightPage.setContent(`
      <form>
        <label for="first_name">First Name</label>
        <input type="text" id="first_name" name="first_name">
        <label for="job_role">Job Role</label>
        <select id="job_role">
          <option value="eng">Engineer</option>
          <option value="mgr">Manager</option>
        </select>
        <label>
          <input type="checkbox" id="agree"> I agree
        </label>
      </form>
    `);

    const field1 = { label: 'First Name', type: 'text' };
    const success1 = await fillFormDirectly(page, field1, 'Deven');
    console.log('Fill First Name:', success1 ? 'SUCCESS' : 'FAILED');

    const field2 = { label: 'Job Role', type: 'select' };
    const success2 = await fillFormDirectly(page, field2, 'Engineer');
    console.log('Fill Job Role:', success2 ? 'SUCCESS' : 'FAILED');

    const field3 = { label: 'I agree', type: 'checkbox' };
    const success3 = await fillFormDirectly(page, field3, 'true');
    console.log('Fill Checkbox:', success3 ? 'SUCCESS' : 'FAILED');

    const values = await page.evaluate(() => ({
      name: document.querySelector('#first_name').value,
      role: document.querySelector('#job_role').value,
      agree: document.querySelector('#agree').checked
    }));
    console.log('Resulting values:', values);

    console.log('\n--- Testing pickElementFromList (via actWithRetry) ---');
    await playwrightPage.setContent(`
      <div>
        <button id="btn1">Save Draft</button>
        <button id="btn2">Submit Application</button>
      </div>
    `);

    // This should trigger pickElementFromList as Layer 1
    const result = await actWithRetry(stagehand, page, 'Click the Submit Application button');
    console.log('Act result:', result);

  } catch (err) {
    console.error('Test failed:', err);
  } finally {
    await stagehand.close();
  }
}

testFixes();
