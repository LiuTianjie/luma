import assert from "node:assert/strict";
import { test } from "node:test";
import { maskValue, serializeDomTree, shouldMaskInput } from "../dist/replay.mjs";

function makeFakeEl({ tag = "div", attrs = {}, children = [], value = "", checked = false, parent = null, rect = { width: 200, height: 48 } }) {
  const attrMap = new Map(Object.entries(attrs));
  const el = {
    nodeType: 1,
    tagName: tag.toUpperCase(),
    parentElement: parent,
    parentNode: parent,
    value,
    checked,
    childNodes: [],
    getAttribute: (k) => (attrMap.has(k) ? attrMap.get(k) : null),
    hasAttribute: (k) => attrMap.has(k),
    getAttributeNames: () => Array.from(attrMap.keys()),
    getBoundingClientRect: () => rect,
    closest(selector) {
      const parts = selector.split(",");
      let cur = el;
      while (cur) {
        for (const p of parts) {
          if (p === "[data-eye-mask]" && cur.hasAttribute?.("data-eye-mask")) return cur;
          if (p === "[data-eye-block]" && cur.hasAttribute?.("data-eye-block")) return cur;
        }
        cur = cur.parentElement;
      }
      return null;
    },
  };
  for (const child of children) {
    child.parentElement = el;
    child.parentNode = el;
    el.childNodes.push(child);
  }
  return el;
}

function makeText(text, parent = null) {
  return {
    nodeType: 3,
    nodeValue: text,
    parentElement: parent,
    parentNode: parent,
    childNodes: [],
  };
}

test("shouldMaskInput balances privacy and debuggability", () => {
  const pwd = makeFakeEl({ tag: "input", attrs: { type: "password" }, value: "hunter2" });
  assert.equal(shouldMaskInput(pwd), true);

  const phone = makeFakeEl({ tag: "input", attrs: { name: "user_mobile" }, value: "13800138000" });
  assert.equal(shouldMaskInput(phone), true);

  const token = makeFakeEl({ tag: "input", attrs: { placeholder: "Enter API Key" }, value: "sk_live_123" });
  assert.equal(shouldMaskInput(token), true);

  const cardByVal = makeFakeEl({ tag: "input", attrs: { name: "note" }, value: "4111 1111 1111 1234" });
  assert.equal(shouldMaskInput(cardByVal), true);

  const search = makeFakeEl({ tag: "input", attrs: { type: "search", name: "q" }, value: "cloudflare kumo" });
  assert.equal(shouldMaskInput(search), false);
  assert.equal(shouldMaskInput(search, true), true);

  assert.equal(maskValue("abc"), "••••••");
});

test("serializeDomTree strips scripts/handlers, resolves URLs, and honors mask/block attributes", () => {
  const titleText = makeText("Dashboard");
  const scriptEl = makeFakeEl({ tag: "script", children: [makeText("alert(1)")] });
  const cssLink = makeFakeEl({ tag: "link", attrs: { rel: "stylesheet", href: "/assets/app.css" } });
  const head = makeFakeEl({ tag: "head", children: [cssLink, scriptEl] });

  const searchInput = makeFakeEl({
    tag: "input",
    attrs: { type: "text", name: "query", oninput: "steal()" },
    value: "status:500",
  });
  const secretInput = makeFakeEl({
    tag: "input",
    attrs: { type: "password", name: "pwd" },
    value: "my-super-secret",
  });
  const maskedDiv = makeFakeEl({
    tag: "div",
    attrs: { "data-eye-mask": "" },
    children: [makeText("pk_secret_999")],
  });
  const blockedDiv = makeFakeEl({
    tag: "div",
    attrs: { "data-eye-block": "" },
    children: [makeText("never serialized")],
    rect: { width: 320, height: 80 },
  });
  const evilLink = makeFakeEl({
    tag: "a",
    attrs: { href: "javascript:evil()", onclick: "bad()" },
    children: [titleText],
  });

  const body = makeFakeEl({
    tag: "body",
    children: [searchInput, secretInput, maskedDiv, blockedDiv, evilLink],
  });
  const html = makeFakeEl({ tag: "html", attrs: { class: "dark" }, children: [head, body] });
  const doc = {
    nodeType: 9,
    baseURI: "https://app.example.com/orders?token=secret123",
    childNodes: [{ nodeType: 10, name: "html", childNodes: [] }, html],
  };

  const { root } = serializeDomTree(doc);
  assert.equal(root.t, 0);
  assert.equal(root.ch.length, 2);

  const htmlNode = root.ch[1];
  const headNode = htmlNode.ch[0];
  // <script> must be stripped, leaving only <link>
  assert.equal(headNode.ch.length, 1);
  assert.equal(headNode.ch[0].a.href, "https://app.example.com/assets/app.css");

  const bodyNode = htmlNode.ch[1];
  const [sIn, pIn, mDiv, bDiv, aLink] = bodyNode.ch;
  assert.equal(sIn.a._val, "status:500");
  assert.equal(sIn.a.oninput, undefined);

  assert.match(pIn.a._val, /^•+$/);
  assert.match(mDiv.ch[0].tx, /^•+$/);

  assert.deepEqual(bDiv.blocked, [320, 80]);
  assert.equal(bDiv.ch, undefined);

  assert.equal(aLink.a.href, "about:blank");
  assert.equal(aLink.a.onclick, undefined);
});
