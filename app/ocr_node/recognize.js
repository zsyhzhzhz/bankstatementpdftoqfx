/**
 * 命令行 OCR 小工具：给一张图片路径，用 tesseract.js 识别文字，
 * 把识别出的每个单词及其像素坐标（在图片里的 left/top/width/height）
 * 以 JSON 数组的形式打印到 stdout，供 Python 端（app/ocr.py）读取。
 *
 * 用法：node recognize.js <图片路径> [语言，默认 eng]
 *
 * 语言训练数据（*.traineddata）会在第一次使用某种语言时自动从网络下载，
 * 并缓存在本目录下（cacheOptions.langPath），后续调用不会重复下载。
 */

const path = require("path");
const { createWorker } = require("tesseract.js");

async function main() {
  const imgPath = process.argv[2];
  const lang = process.argv[3] || "eng";
  if (!imgPath) {
    process.stderr.write("用法: node recognize.js <图片路径> [语言]\n");
    process.exit(2);
  }

  const worker = await createWorker(lang, 1, {
    // 语言数据缓存在本目录，避免每次都要重新联网下载
    cachePath: __dirname,
    langPath: __dirname,
  });
  try {
    const { data } = await worker.recognize(imgPath, {}, { tsv: true });
    const words = [];
    for (const rawLine of (data.tsv || "").split("\n")) {
      const cols = rawLine.split("\t");
      if (cols.length < 12) continue;
      const level = +cols[0];
      if (level !== 5) continue; // level 5 = 单词级别的行，才有实际文字和坐标
      const [, , blockNum, parNum, lineNum, wordNum, left, top, width, height, conf, ...rest] = cols;
      const text = rest.join("\t");
      if (!text || !text.trim()) continue;
      words.push({
        text,
        block: +blockNum,
        par: +parNum,
        line: +lineNum,
        word: +wordNum,
        x0: +left,
        y0: +top,
        x1: +left + +width,
        y1: +top + +height,
        conf: +conf,
      });
    }
    process.stdout.write(JSON.stringify(words));
  } finally {
    await worker.terminate();
  }
}

main().catch((e) => {
  process.stderr.write(String((e && e.stack) || e) + "\n");
  process.exit(1);
});
