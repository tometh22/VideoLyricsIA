import puppeteer from 'puppeteer';
import { fileURLToPath } from 'url';
import path from 'path';
import fs from 'fs';

const __dirname = path.dirname(fileURLToPath(import.meta.url));

(async () => {
  const browser = await puppeteer.launch({
    headless: true,
    executablePath: '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  });
  const page = await browser.newPage();
  await page.setViewport({ width: 1280, height: 720, deviceScaleFactor: 2 });

  const htmlPath = path.join(__dirname, 'compliance.html');
  await page.goto(`file://${htmlPath}`, { waitUntil: 'networkidle0', timeout: 30000 });
  await new Promise(r => setTimeout(r, 3000));

  // Tighter padding for PDF
  await page.evaluate(() => {
    document.querySelectorAll('.slide').forEach(s => {
      s.style.padding = '40px 50px';
    });
  });

  // Get all slides
  const slideCount = await page.$$eval('.slide', els => els.length);
  console.log(`Found ${slideCount} slides`);

  // Screenshot each slide individually
  const screenshotPaths = [];
  for (let i = 0; i < slideCount; i++) {
    const ssPath = path.join(__dirname, `_compliance_slide_${i + 1}.png`);

    await page.evaluate((idx) => {
      const slides = document.querySelectorAll('.slide');
      slides[idx].scrollIntoView({ behavior: 'instant' });
    }, i);
    await new Promise(r => setTimeout(r, 500));

    await page.screenshot({ path: ssPath, type: 'png' });
    screenshotPaths.push(ssPath);
    console.log(`  Slide ${i + 1}/${slideCount}`);
  }

  // Create PDF from screenshots
  const imagesHtml = screenshotPaths.map((p) => `
    <div style="width:100vw;height:100vh;page-break-after:always;overflow:hidden;margin:0;padding:0">
      <img src="file://${p}" style="width:100%;height:100%;display:block;object-fit:cover" />
    </div>
  `).join('');

  const pdfHtmlPath = path.join(__dirname, '_compliance_pdf_temp.html');
  fs.writeFileSync(pdfHtmlPath, `<!DOCTYPE html><html><head>
    <style>
      * { margin:0; padding:0; }
      @page { size: 1280px 720px; margin: 0; }
      body { margin:0; padding:0; }
    </style>
  </head><body>${imagesHtml}</body></html>`);

  const pdfPage = await browser.newPage();
  await pdfPage.setViewport({ width: 1280, height: 720 });
  await pdfPage.goto(`file://${pdfHtmlPath}`, { waitUntil: 'networkidle0' });
  await new Promise(r => setTimeout(r, 1000));

  const outputPath = path.join(__dirname, 'GenLy_AI_Compliance_Report.pdf');
  await pdfPage.pdf({
    path: outputPath,
    width: '1280px',
    height: '720px',
    printBackground: true,
    margin: { top: 0, right: 0, bottom: 0, left: 0 },
  });

  // Cleanup temp files
  screenshotPaths.forEach(p => fs.unlinkSync(p));
  fs.unlinkSync(pdfHtmlPath);

  console.log(`\nPDF generated with ${slideCount} pages: GenLy_AI_Compliance_Report.pdf`);
  await browser.close();
})();
