import test from "node:test";
import assert from "node:assert/strict";
import { __test } from "./worker.js";

test("colab relay snapshot redacts provider credentials", () => {
  const out = __test.redactColabSnapshot({
    type: "progress",
    job: {
      payload: { source: { credentials: { access_token: "x" } } },
      logs: Array.from({ length: 60 }, (_, i) => `line ${i}`),
    },
  });

  assert.equal(out.job.payload.source.credentials, undefined);
  assert.equal(out.job.logs.length, 50);
});
