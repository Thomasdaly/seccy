# Seccy dashboard frontend audit

The dashboard now opens with service reachability, failing and warning checks, 24-hour LLM spend, and free disk space. In the synthetic demo, all nine services answer while three checks need attention. Showing both numbers makes that distinction visible on the first screen.

The six views share a responsive light and dark theme, named page headings, and keyboard navigation. The dashboard ships four local frontend files with system fonts and no frontend build step or third-party assets. The service badge appears on Health, so opening Trends, Findings, or another view does not also request a health snapshot.

## Review findings

The locations below point to the updated implementation. The baseline frontend was commit `3660627`.

| Severity | Location | Failure scenario in the baseline | Change |
| --- | --- | --- | --- |
| High | [app.js:389](../dashboard/static/app.js#L389) | A Langfuse metrics failure left zero defaults in the response. An operator could read `$0` spend and zero errors as measured results. | Trends marks usage unavailable and keeps the independently sourced findings count visible. |
| Medium | [app.js:495](../dashboard/static/app.js#L495) | Flows described every judge rejection as a false positive, even when there was no ground-truth determination. | The summary reports the observed rejection share. Eval retains its ground-truth-based false-positive counts. |
| Medium | [app.js:1017](../dashboard/static/app.js#L1017) | A failed scheduled refresh removed the last usable data. A slow response could also build charts after the operator had changed the view. | Failed refreshes retain a matching snapshot and offer Retry. Superseded requests are cancelled and only the current response mounts charts. |
| Medium | [app.js:602](../dashboard/static/app.js#L602) | A slow run request reopened the drawer after it was dismissed. The non-modal drawer also let keyboard focus move behind it. | Closing the native dialog cancels the request and restores focus. The dialog contains keyboard focus while open. |
| Medium | [style.css:147](../dashboard/static/style.css#L147) | At 320 px, fixed minimum widths pushed controls or cards off-screen in Health, Flows, and Eval. Eval also overflowed at 390 px. | Cards and navigation reflow. Wide tables retain their own keyboard-scrollable viewport. |
| Medium | [index.html:23](../dashboard/static/index.html#L23) | Tabs exposed selection state without a linked panel or arrow-key navigation. An operator also could not use Back to return to the previous dashboard view. | Tabs link to their panels and support arrow keys, Home, End, and Enter. View and range changes participate in browser history. |
| Medium | [style.css:2](../dashboard/static/style.css#L2) | Light-theme labels and table text lost contrast in dense views, including the status descriptions on Health. | Shared foreground and surface colours cover both themes. Focus indicators and status text supplement colour. |
| Medium | [charts.js:156](../dashboard/static/charts.js#L156) | A keyboard user could tab through 36 chart marks on the default Trends view before reaching later content. | Each populated trend chart takes one tab stop. Arrow keys explore its values and a Table control exposes the data in rows. |
| Medium | [app.js:720](../dashboard/static/app.js#L720) | Selecting a Findings filter rebuilt the filter buttons and dropped focus. A search with zero matches still offered an export. | Filter buttons retain focus, report the matching result count, and disable empty export. |
| Low | [app.js:268](../dashboard/static/app.js#L268) | A healthy service badge could dominate the first impression while a failed evidence check appeared among routine rows. | Separate summary tiles show service reachability and checks needing attention. The checks panel lists failures and warnings first. |

## Before-and-after measurements

The same Chromium 157 browser and loopback synthetic API served both versions. The sweep covered Health, Trends, Flows, Findings, Eval, and all five Raw subviews at 1440 px light, 320 px light, and 390 px dark. The default range was 7d. The accessibility scan used axe rules tagged WCAG 2.0 and 2.1 A/AA, WCAG 2.2 AA, and best practices. An element flagged in several renders contributes one occurrence per render. Keyboard operation was checked separately. [WCAG 2.2](https://www.w3.org/TR/WCAG22/) · [ARIA tabs pattern](https://www.w3.org/WAI/ARIA/apg/patterns/tabs/) · [ARIA modal dialog pattern](https://www.w3.org/WAI/ARIA/apg/patterns/dialog-modal/)

| Measure | Baseline | Updated |
| --- | ---: | ---: |
| Scans with axe violations | 30 of 30 | 0 of 30 |
| Flagged axe element occurrences | 937 | 0 |
| Renders with page-level horizontal overflow | 4 of 30 | 0 of 30 |
| Worst page-level horizontal overflow | 88 px | 0 px |
| Peak observed layout shift | 0.131 | 0.025 |
| Median observed largest contentful paint | 68 ms | 52 ms |
| Median initial API reads per view | 3 | 2 |
| Default Trends chart tab stops | 36 | 6 |
| Browser console errors or uncaught exceptions | 0 | 0 |
| Static frontend size, uncompressed | 102,910 B | 116,081 B |
| Estimated gzip size | 29,679 B | 33,423 B |
| Production frontend dependencies added | 0 | 0 |

LCP had 29 baseline samples and 30 updated samples. These are single-pass loopback measurements. Tailnet performance and 75th-percentile Core Web Vitals require field data. Automated accessibility coverage is limited to the sampled views and rules. Manual screen-reader and cross-browser verification remain open. [Core Web Vitals](https://web.dev/articles/vitals)

All 12 browser interaction checks passed. They covered the Health summary, tab keyboard controls and Back, all four ranges, failed refresh and initial-load retry, Findings filter focus, record-dialog focus and Escape, unavailable usage, chart arrows and the table alternative, delayed range responses, run deep links, and closing a run before its response arrived. Raw SQL and route-probe controls were inspected as UI. Browser requests during the sweep were limited to loopback GETs.

## Synthetic dashboard snapshot

| Window | Sample metrics |
| --- | --- |
| Health | 9 of 9 services reachable. 6 of 9 checks passing, 2 warning, 1 failing. 31% disk free. |
| 24 hours | $1.44 spend across 468 traced calls. |
| Default 7d view | $6.93 spend across 2,178 calls and 4,609,559 tokens. 42 errored calls, a 1.93% error rate. $3.18 per 1,000 calls. |
| Findings and Eval | 90 unique evidenced findings, 22 verdicts needing review, 1 confirmation without evidence, and 7 scored lab runs. |

The demo values illustrate the interface. Selected time ranges are aligned to buckets and include the current partial bucket. Cost per 1,000 calls divides the selected window's spend by traced calls and is unavailable without usage data or a call denominator.

## Recommended next work

1. **Persist service health history and add a compact incident timeline.** Record service degradation, recovery, and data-source outages on the same timeline. Today the 24-hour service history starts over when the dashboard restarts, which can erase the context for an outage just as an operator needs it. [backend.py:314](../dashboard/backend.py#L314)
2. **Page large Findings result sets on the server.** The browser currently receives the range's verdicts and filters them locally. A long window with enough verdicts can produce a large response and a slow Show All action. Preserve the filtered count and export query when introducing pagination. [backend.py:987](../dashboard/backend.py#L987) · [app.js:174](../dashboard/static/app.js#L174)
3. **Clarify metric aggregation in Trends.** The API selects the highest model p95 within a route when multiple models contribute. A slow, low-volume model can dominate that statistic. Label it as the highest model p95 or calculate the pooled percentile at the source. Add explicit window boundaries before comparing consecutive periods. [backend.py:871](../dashboard/backend.py#L871) · [app.js:384](../dashboard/static/app.js#L384)
