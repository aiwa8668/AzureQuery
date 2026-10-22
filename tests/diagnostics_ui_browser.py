import json
import pathlib
import re
import sys
import unittest

from playwright.sync_api import expect, sync_playwright


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIELDS = "TimeGenerated RequestId OperationName CallerIPAddress DurationMs ResultSignature event_s threadID_d PromptTokens CachedTokens CacheWriteTokens GeneratedTokens LongContextPromptTokens LongContextCachedTokens LongContextCacheWriteTokens LongContextGeneratedTokens Model InputTokens OutputTokens CachedInputTokens UncachedInputTokens properties_s".split()
REMOVED = "CorrelationId EnqueueTime_t FluentdIngestTimestamp_s location_s processID_d Deployment ModelVersion".split()


class DiagnosticsUIBrowserTests(unittest.TestCase):
    def assert_compact_columns(self, page):
        widths = page.locator("#diagnosticsTable table").evaluate("""table => {
          return Array.from(table.tHead.rows[0].cells, (header, index) => {
            const cells = Array.from(table.rows, row => row.cells[index]);
            const longest = Math.max(...cells.map(cell => {
              const range = document.createRange();
              range.selectNodeContents(cell);
              const style = getComputedStyle(cell);
              return range.getBoundingClientRect().width + parseFloat(style.paddingLeft) + parseFloat(style.paddingRight) + 1;
            }));
            return {key: header.title, actual: header.getBoundingClientRect().width, longest,
              singleLine: cells.every(cell => {
                const range = document.createRange();
                range.selectNodeContents(cell);
                return range.getClientRects().length === 1 && getComputedStyle(cell).whiteSpace === 'nowrap';
              })};
          });
        }""")
        for column in widths:
            with self.subTest(column=column["key"], viewport=page.viewport_size):
                self.assertTrue(column["singleLine"])
                self.assertAlmostEqual(column["actual"], column["longest"], delta=2, msg=str(column))
        return widths

    def test_fields_labels_scroll_and_cleanup(self):
        sys.path.insert(0, str(ROOT))
        import main

        html = main.app.test_client().get("/").get_data(as_text=True)
        html = re.sub(r'<script src="[^"]+"></script>', "", html)
        html = html.replace("</body>", '<script src="/static/js/app-shell.js"></script><script src="/static/js/azure-diagnostics.js"></script></body>')
        resource = {"id": "/subscriptions/test/resourceGroups/rg/providers/Microsoft.CognitiveServices/accounts/ai", "name": "foundry-test", "diagnosticStatus": "pending"}
        self.empty_rows = False
        self.empty_fields = False
        self.diagnostic_status = "known"

        def route_request(route):
            path = route.request.url.split("diagnostics.test", 1)[-1]
            if path.startswith("/static/"):
                file = ROOT / "public" / path.split("?", 1)[0].removeprefix("/static/")
                route.fulfill(path=str(file))
                return
            if path.startswith("/api/openai-tier/auth/status"):
                data = {"configured": True, "signedIn": True}
            elif path.startswith("/api/openai-tier/subscriptions"):
                data = {"subscriptions": [{"subscriptionId": "test", "subscriptionName": "测试"}]}
            elif path.startswith("/api/azure-diagnostics/workspaces"):
                data = {"workspaces": [{"id": "logs", "name": "logs"}]}
            elif path.startswith("/api/azure-diagnostics/resources"):
                if "phase=diagnostics" in path:
                    data = {"resources": [{**resource, "diagnosticStatus": self.diagnostic_status, "logsEnabled": True, "usageLogsEnabled": True, "workspaceIds": ["logs"]}]}
                else:
                    data = {"resources": [resource]}
            elif path.startswith("/api/azure-diagnostics/query"):
                row = {key: f"{key}-test" for key in FIELDS + REMOVED}
                row["TimeGenerated"] = "2026-10-21T00:00:00Z"
                row["RequestId"] = "12345678-abcd-1234-abcd-123456789abc"
                row["OperationName"] = "ChatCompletions"
                row["PromptTokens"] = 1
                row["Model"] = "m"
                row["properties_s"] = {"model": "gpt-4.1", "details": "调用日志 detail " * 1500}
                second_row = {**row, "OperationName": "ChatCompletionsWithLongerOperationName", "properties_s": {"ok": True}}
                data = {"rows": [] if self.empty_rows else ([{"unexpected": 1}] if self.empty_fields else [row, second_row]), "columns": [] if self.empty_fields else list(reversed(FIELDS + REMOVED))}
            else:
                route.fulfill(body=html, content_type="text/html")
                return
            route.fulfill(body=json.dumps(data), content_type="application/json")

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            page = browser.new_page(viewport={"width": 1280, "height": 900}, accept_downloads=True)
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.add_init_script("""(() => {
              const NativeObserver = window.ResizeObserver;
              window.uiObservers = [];
              window.uiScrollListeners = new Set();
              window.ResizeObserver = class extends NativeObserver {
                constructor(callback) { super(callback); this.callback = callback; this.active = true; window.uiObservers.push(this); }
                disconnect() { this.active = false; super.disconnect(); }
              };
              const add = EventTarget.prototype.addEventListener;
              const remove = EventTarget.prototype.removeEventListener;
              EventTarget.prototype.addEventListener = function(type, listener, options) {
                if (type === 'scroll' && ['diagnosticsTable', 'diagnosticsTableScroll'].includes(this.id)) window.uiScrollListeners.add(listener);
                return add.call(this, type, listener, options);
              };
              EventTarget.prototype.removeEventListener = function(type, listener, options) {
                if (type === 'scroll' && ['diagnosticsTable', 'diagnosticsTableScroll'].includes(this.id)) window.uiScrollListeners.delete(listener);
                return remove.call(this, type, listener, options);
              };
            })();""")
            page.route("**/*", route_request)
            page.goto("http://diagnostics.test/?view=azure-diagnostics")
            option = page.locator("#diagnosticsResource option")
            expect(option).to_have_text("✅ 已启用日志/foundry-test")
            self.assertEqual(option.evaluate("e => e.style.color"), "rgb(21, 128, 61)")
            expect(page.locator("#diagnosticsQuery")).to_be_enabled()
            page.locator("#diagnosticsQuery").click()
            expect(page.locator("#diagnosticsTable th")).to_have_count(22)
            self.assertEqual(page.locator("#diagnosticsTable th").evaluate_all("items => items.map(e => e.title)"), FIELDS)
            self.assertEqual(page.locator("#diagnosticsFieldsList input").evaluate_all("items => items.map(e => e.dataset.field)"), FIELDS)
            full_widths = self.assert_compact_columns(page)
            sys.stdout.write("\n列宽实测：" + json.dumps([column for column in full_widths if column["key"] in ("RequestId", "OperationName")], ensure_ascii=False) + "\n")
            page.locator("#diagnosticsFieldsToggle").click()
            self.assertEqual(page.locator("#diagnosticsFieldsList label").first.evaluate("e => getComputedStyle(e).paddingTop"), "3px")
            page.locator('#diagnosticsFieldsList input[data-field="properties_s"]').uncheck()
            compact_widths = self.assert_compact_columns(page)
            for before, after in zip(full_widths, compact_widths):
                self.assertAlmostEqual(before["actual"], after["actual"], delta=1)
            page.locator('#diagnosticsFieldsList input[data-field="properties_s"]').check()
            page.locator("#diagnosticsFieldsToggle").click()
            scrollbar = page.locator("#diagnosticsTableScroll")
            expect(scrollbar).to_be_visible()
            scrollbar.evaluate("e => e.scrollLeft = 300")
            page.wait_for_function("diagnosticsTable.scrollLeft === diagnosticsTableScroll.scrollLeft && diagnosticsTable.scrollLeft > 0")
            page.locator("#diagnosticsTable").evaluate("e => e.scrollLeft = 650")
            page.wait_for_function("diagnosticsTableScroll.scrollLeft === diagnosticsTable.scrollLeft && diagnosticsTableScroll.scrollLeft > 300")
            page.set_viewport_size({"width": 850, "height": 900})
            page.wait_for_function("diagnosticsTableScroll.clientWidth === diagnosticsTable.clientWidth && diagnosticsTableScroll.scrollWidth === diagnosticsTable.scrollWidth")
            self.assert_compact_columns(page)
            page.locator("#diagnosticsTable td").last.evaluate("e => e.textContent = 'x'.repeat(2000)")
            page.wait_for_function("parseFloat(diagnosticsTableScrollSpacer.style.width) === diagnosticsTable.scrollWidth")
            self.assert_compact_columns(page)
            with page.expect_download() as download:
                page.locator("#diagnosticsDownload").click()
            csv = pathlib.Path(download.value.path()).read_text(encoding="utf-8-sig")
            headers = page.locator("#diagnosticsTable th").all_text_contents()
            self.assertEqual(csv.splitlines()[0], ",".join(f'"{item}"' for item in headers))
            page.locator("#diagnosticsFieldsToggle").click()
            for _ in range(3):
                page.locator("#diagnosticsFieldsNone").click()
                expect(scrollbar).to_be_hidden()
                self.assertEqual(page.evaluate("uiObservers.filter(o => o.active).length"), 0)
                self.assertEqual(page.evaluate("uiScrollListeners.size"), 0)
                page.evaluate("uiObservers.forEach(o => o.callback())")
                self.assertEqual(page.locator("#diagnosticsTableScrollSpacer").evaluate("e => e.style.width"), "0px")
                page.locator("#diagnosticsFieldsAll").click()
                expect(scrollbar).to_be_visible()
                self.assertEqual(page.evaluate("uiObservers.filter(o => o.active).length"), 1)
                self.assertEqual(page.evaluate("uiScrollListeners.size"), 2)
                self.assert_compact_columns(page)
            page.locator('#diagnosticsFieldsList input[data-field="TimeGenerated"]').uncheck()
            self.assertEqual(page.locator("#diagnosticsTable th").first.get_attribute("title"), "RequestId")
            self.assert_compact_columns(page)
            page.locator("#diagnosticsFieldsNone").click()
            page.locator('#diagnosticsFieldsList input[data-field="PromptTokens"]').check()
            self.assert_compact_columns(page)
            expect(scrollbar).to_be_hidden()
            page.locator("#diagnosticsFieldsNone").click()
            page.locator('#diagnosticsFieldsList input[data-field="Model"]').check()
            self.assert_compact_columns(page)
            expect(scrollbar).to_be_hidden()
            page.locator("#diagnosticsFieldsToggle").click()
            page.locator("#diagnosticsStart").dispatch_event("change")
            expect(scrollbar).to_be_hidden()
            self.assertEqual(page.evaluate("uiScrollListeners.size"), 0)
            self.empty_rows = True
            page.locator("#diagnosticsQuery").click()
            expect(page.locator("#diagnosticsTable .empty")).to_contain_text("暂无调用日志")
            expect(scrollbar).to_be_hidden()
            self.empty_rows = False
            self.empty_fields = True
            page.locator("#diagnosticsQuery").click()
            expect(page.locator("#diagnosticsTable .empty")).to_have_text("查询结果没有可用字段。")
            self.assertEqual(page.evaluate("uiObservers.filter(o => o.active).length"), 0)
            self.diagnostic_status = "unknown"
            page.locator("#diagnosticsRefresh").click()
            expect(option).to_have_text("? 状态未知/foundry-test")
            self.assertEqual(option.evaluate("e => e.style.color"), "")
            self.assertEqual(errors, [])
            browser.close()


if __name__ == "__main__":
    unittest.main()
