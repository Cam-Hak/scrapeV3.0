<?php
/**
 * Drop-in snippet: crawl status, in your own layout.
 *
 * `status.php` returns arrays and emits nothing. Everything below the fetch is
 * yours to replace - the markup here is a plain table so the snippet runs, not
 * a suggestion. Delete it and use your own components, your own CSS, your own
 * grid; only the three rules at the bottom of this comment carry over.
 *
 * Copy `status.php` and this file into the site. Nothing else is needed: no
 * shared code with the crawler, no Python, no file paths into its tree.
 *
 * The rules that do carry over:
 *
 *   1. Switch on `severity` (only ever ok / warn / error). Display `health`.
 *      A health word the crawler learns later resolves to warn on a site
 *      nobody has redeployed; a page that colours health words itself renders
 *      that one uncoloured, which reads as "fine".
 *   2. Print `reason` verbatim. It is already a sentence, and re-deriving it
 *      means a second definition of "healthy" that drifts from the crawler's.
 *   3. Show `updated_at`. The table is refreshed by a batch job that can stop
 *      running, and a frozen grid looks exactly like a healthy one.
 */

declare(strict_types=1);

require __DIR__ . '/status.php';

$pdo = scrapev3_connect(
    getenv('SCRAPEV3_DB_HOST') ?: 'localhost',
    getenv('SCRAPEV3_DB_USER') ?: 'website',
    (string) getenv('SCRAPEV3_DB_PASSWORD')
);

// Filters and sort come straight from the query string. `sort` is checked
// against a whitelist inside status.php and never interpolated, so passing a
// user-supplied value here is safe; an unknown one throws.
$rows = scrapev3_statuses($pdo, [
    'severity' => $_GET['severity'] ?? null,   // ok | warn | error
    'health'   => $_GET['health']   ?? null,   // healthy, empty, stale, ...
    'search'   => $_GET['q']        ?? null,   // domain or newsroom URL
    'uncached' => isset($_GET['uncached']),    // discovery never solved it
    'due'      => isset($_GET['due']),         // the schedule picks it up next
    'sort'     => $_GET['sort']     ?? null,   // any column; default worst-first
    'desc'     => isset($_GET['desc']),
], 100);

$summary = scrapev3_summary($pdo);

// Colour is the site's business; the mapping is not. Three keys, forever.
$colour = ['ok' => '#1a7f37', 'warn' => '#9a6700', 'error' => '#b3261e'];
$e = static fn(?string $v): string => htmlspecialchars((string) $v, ENT_QUOTES, 'UTF-8');
?>

<p>
  <?= $summary['total'] ?> agencies &middot;
  <?= $summary['severity']['error'] ?> error,
  <?= $summary['severity']['warn'] ?> warn,
  <?= $summary['severity']['ok'] ?> ok &middot;
  updated <?= $e($summary['updated_at'] ?? 'never') ?>
</p>

<table>
  <thead>
    <tr>
      <?php foreach ([
        'a_id' => 'a_id', 'domain' => 'Site', 'health' => 'Health',
        'discovery_method' => 'Source', 'articles' => 'Articles',
        'last_stored_at' => 'Last pulled', 'next_due_at' => 'Next due',
      ] as $key => $label): ?>
        <?php // Clicking a header flips the direction it is already sorted by.
              $flip = (($_GET['sort'] ?? '') === $key && !isset($_GET['desc']))
                    ? "&desc=1" : ""; ?>
        <th><a href="?sort=<?= $e($key) . $flip ?>"><?= $e($label) ?></a></th>
      <?php endforeach; ?>
      <th>Why</th>
    </tr>
  </thead>
  <tbody>
  <?php foreach ($rows as $r): ?>
    <tr>
      <td><?= $r['a_id'] ?></td>
      <td>
        <strong><?= $e($r['domain']) ?></strong><br>
        <small><?= $e($r['newsroom_url']) ?></small>
      </td>

      <!-- Rule 1: colour from severity, label from health. -->
      <td style="color: <?= $colour[$r['severity']] ?? '#57606a' ?>">
        <?= $e($r['health']) ?>
      </td>

      <!-- `targets_cached < targets` means discovery has not solved every
           newsroom this agency owns. It is not a health word: an agency can be
           healthy and still have an unsolved page, so nothing else flags it. -->
      <td>
        <?php if (!$r['discovery_method']): ?>
          <em>not solved</em>
        <?php elseif ($r['targets_cached'] < $r['targets']): ?>
          <?= $e($r['discovery_method']) ?>
          (<?= $r['targets_cached'] ?>/<?= $r['targets'] ?>)
        <?php else: ?>
          <?= $e($r['discovery_method']) ?>
        <?php endif; ?>
      </td>

      <td><?= $r['articles'] ?></td>

      <!-- `last_stored_at` is when WE last pulled a document. `last_article_at`
           is the publisher's own date - a site republishing old items looks
           fresh by that one, and a site we stopped storing looks fine by it. -->
      <td><?= $e($r['last_stored_at'] ?? 'never') ?></td>
      <td><?= $e($r['next_due_at'] ?? 'unknown') ?></td>

      <!-- Rule 2: verbatim. -->
      <td><?= $e($r['reason']) ?></td>
    </tr>
  <?php endforeach; ?>
  </tbody>
</table>
