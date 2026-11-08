# Privacy

Routing and redaction are one pipeline, and one area of the console: **Privacy**.
The two topics are documented separately: [Privacy routing](privacy-routing.md)
(how a request is handled) and [Redaction](redaction.md) (what is hidden).

## The pipeline: four steps, two kinds of rules

Every request that carries text goes through the same four steps, in this order.

1. **Route.** Routing rules, per kind of request (reading a file, embedding, chat
   later): the **first rule that matches wins**. It picks the reader for a file,
   whether text may go to a cloud model, or a refusal.
2. **Detect.** The detector profile the route chose (the default, **Fast**, is
   the Presidio engine); patterns always run.
3. **Hide.** Redaction rules decide what is hidden. **Every matching rule
   applies and the strictest mode wins**, then the user's own rules, which can
   only tighten. A routing rule's "add a policy" adds one more set of rules at
   this step.
4. **Send or refuse.** What is left goes to the provider, or the request is
   refused.

Routing decides how a request is handled; redaction rules decide what is hidden.
Routing never lowers a redaction mode.

| | Routing rules | Redaction rules |
|---|---|---|
| Purpose | how a request is handled: reader, local or cloud, refuse | what is hidden from the provider |
| Match on | kind of request, file type and name, who asked, target model or provider, what the text contains | scope: the deployment, a provider, a model, a group, a user or a key |
| Several match | the **first** one applies (observe-only rules are logged and the search goes on) | **all** apply, and the **strictest** mode wins |
| Can do | choose a reader, embed on a local model, refuse | per entity type: leave, redact, anonymise (with or without restoring the answer), block |
| Lives in | console: **Privacy, Rules** | console: **Privacy, What gets hidden** |

## The console

| Tab | What it holds | Step |
|---|---|---|
| **Rules** | routing rules, per kind, in first-match order | 1 Route |
| **What gets hidden** | redaction rules: scopes, a mode per entity type, custom patterns, same-length placeholders, the labels the model is told | 3 Hide |
| **Detectors** | detector profiles; the Fast profile carries the engine choice (with its append-only record) and the Presidio pattern and named-entity switches | 2 Detect |
| **Test** | one box: a kind of request, its facts and a text or file, and the four steps in order. Nothing is stored | all |

Old links to `/admin/redaction` and `/admin/privacy/profiles` redirect to their new places.
