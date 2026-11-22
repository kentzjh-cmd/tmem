// 证明 tmem 0.4.1 的 zstd 修复在"单帧合成数据"上绿、在 DSH 真实的"拼接多帧"数据上失效。
import * as zlib from "node:zlib";
import { readFileSync, writeFileSync } from "node:fs";
import { execFileSync } from "node:child_process";
import { DshAdapter } from "/archive/tmem/tmem/adapters/dsh.mjs";

const adapter = new DshAdapter();
const OUT = "/tmp/tmem-zstd-ab";

// 复刻 DSH 的拼接帧容器：每批 append 一个独立 zstd frame
function dshStyleContainer(lines, batchSize = 8) {
  const chunks = [];
  for (let i = 0; i < lines.length; i += batchSize) {
    const batch = Buffer.from(lines.slice(i, i + batchSize).join("\n") + "\n", "utf-8");
    chunks.push(zlib.zstdCompressSync(batch));
  }
  return Buffer.concat(chunks);
}

const stat = (p) => {
  const s = adapter.parseTranscript(p, "/tmp/tmem-validation/F1/ws2-1hFjWP");
  return { actions: s.actions.length, intents: s.user_intents.length };
};

// ---- 1) 真实 DSH 多帧文件 ----
const realZstd =
  "/home/kent/.dsh/sessions/--tmp-tmem-validation-F1-ws2-1hFjWP--/session-0c9e346c-db72-426f-99e1-4d35e3f10f94/session.v4.jsonl.zstd";
const realBuf = readFileSync(realZstd);

// 正确基准：zstd CLI（与 DSH 内部逐帧解码等价）
const plainText = execFileSync("zstd", ["-dc", realZstd], { encoding: "utf-8" });
const realPlain = `${OUT}/real-decoded.jsonl`;
writeFileSync(realPlain, plainText);
const lines = plainText.split("\n").filter(Boolean);

// ---- 2) 合成单帧（= 远端 e2e 测试里 zstdCompressSync 的形态）----
const single = `${OUT}/single.jsonl.zstd`;
writeFileSync(single, zlib.zstdCompressSync(Buffer.from(lines.join("\n") + "\n", "utf-8")));

// ---- 3) 合成多帧（= DSH 真实形态）----
const multi = `${OUT}/multi.jsonl.zstd`;
writeFileSync(multi, dshStyleContainer(lines));

console.log("真实 DSH zstd 文件字节        :", realBuf.length);
console.log("zstd -dc 解出后字节 / 行数     :", Buffer.byteLength(plainText), "/", lines.length);
console.log("Node 一次解压得到字节          :", zlib.zstdDecompressSync(realBuf).length, "(仅首帧)");
console.log();
console.log("A 真实 DSH 多帧 → tmem 0.4.1   :", JSON.stringify(stat(realZstd)));
console.log("B 同文件 zstd -dc 明文 → tmem   :", JSON.stringify(stat(realPlain)));
console.log("C 合成 单帧   → tmem 0.4.1     :", JSON.stringify(stat(single)), "  ← 远端 e2e 测试覆盖的形态");
console.log("D 合成 多帧   → tmem 0.4.1     :", JSON.stringify(stat(multi)), "  ← DSH 真实形态");
