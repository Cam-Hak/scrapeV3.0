<?php
/**
 * TESTING PAGE - NOT FOR PRODUCTION.
 *
 * A worked example of rendering `status.php`, so the data can be looked at
 * before any of it is wired into the real site. Deliberately one self-contained
 * file with its own inline CSS: it is meant to be copied from and thrown away,
 * not included, extended, or deployed.
 *
 * It runs with no database. With none configured it renders
 * `sample_status.json` - real rows from an actual crawl, written by
 * `scrapev3 status --json` - so the page can be opened on any PHP host and the
 * shape of the data judged immediately. Point it at MySQL by setting the three
 * environment variables below and it renders live rows through exactly the same
 * code path.
 *
 *     php -S localhost:8000 -t clients      # then open /status_demo.php
 *
 * What to copy into the real site: the severity-to-colour lookup, and the fact
 * that `reason` is displayed verbatim rather than re-derived. What NOT to copy:
 * this file's layout, its inline styles, and its habit of catching every
 * exception and carrying on.
 */

declare(strict_types=1);

require __DIR__ . '/status.php';

const DEMO_FIXTURE = __DIR__ . '/sample_status.json';

/**
 * Live rows when a database is configured, the fixture otherwise.
 *
 * The source is reported back to the page rather than hidden, because a demo
 * that silently falls back to canned data is a demo that will eventually be
 * mistaken for a working dashboard.
 */
function demo_load(array $filters): array
{
    $host = getenv('SCRAPEV3_DB_HOST');
    if (!$host) {
        return [scrapev3_grid_from_file(DEMO_FIXTURE), 'fixture', null];
    }
    try {
        $pdo = scrapev3_connect(
            $host,
            (string) (getenv('SCRAPEV3_DB_USER') ?: 'website'),
            (string) (getenv('SCRAPEV3_DB_PASSWORD') ?: '')
        );
        return [scrapev3_grid($pdo, $filters, 500), 'database', null];
    } catch (Throwable $e) {
        // Fall back so the page still renders, but say so loudly: an empty grid
        // and a healthy grid must never look the same.
        return [scrapev3_grid_from_file(DEMO_FIXTURE), 'fixture', $e->getMessage()];
    }
}

$filters = [
    'severity' => $_GET['severity'] ?? null,
    'health'   => $_GET['health']   ?? null,
    'search'   => $_GET['search']   ?? null,
];
[$grid, $source, $error] = demo_load(array_filter($filters));

$rows = $grid['agencies'];
// The fixture has no WHERE clause, so filtering happens here for it. Live rows
// arrive already filtered and pass through unchanged.
if ($source === 'fixture') {
    $rows = array_values(array_filter($rows, static function (array $r) use ($filters): bool {
        if (!empty($filters['severity']) && $r['severity'] !== $filters['severity']) {
            return false;
        }
        if (!empty($filters['health']) && $r['health'] !== $filters['health']) {
            return false;
        }
        if (!empty($filters['search'])
            && stripos($r['domain'], (string) $filters['search']) === false) {
            return false;
        }
        return true;
    }));
}

$counts   = [];
$bands    = [];   // health word -> its severity, learned from the rows
foreach ($grid['agencies'] as $r) {
    $counts[$r['health']] = ($counts[$r['health']] ?? 0) + 1;
    $bands[$r['health']]  = $r['severity'];
}
arsort($counts);

/** Severity, not health, decides the colour - see status.php. */
function demo_colour(string $severity): string
{
    return ['ok' => '#1a7f37', 'warn' => '#9a6700', 'error' => '#b3261e'][$severity]
        ?? '#57606a';
}

function h(?string $value): string
{
    return htmlspecialchars((string) $value, ENT_QUOTES, 'UTF-8');
}

function demo_ago(?string $timestamp): string
{
    if (!$timestamp) {
        return 'never';
    }
    $days = (int) floor((time() - strtotime($timestamp . ' UTC')) / 86400);
    if ($days <= 0) {
        return 'today';
    }
    return $days === 1 ? '1 day ago' : "$days days ago";
}
?>
<!doctype html>
<meta charset="utf-8">
<title>scrapev3 status (demo)</title>
<style>
  :root { color-scheme: light dark; }
  body { font: 14px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
         margin: 2rem auto; max-width: 1100px; padding: 0 1rem; }
  h1 { font-size: 1.3rem; margin: 0 0 .25rem; }
  .sub { color: #57606a; margin: 0 0 1.5rem; }
  .banner { border-left: 3px solid #9a6700; background: #fff8e5; color: #4d3800;
            padding: .6rem .9rem; margin-bottom: 1.25rem; border-radius: 3px; }
  .banner code { background: rgba(0,0,0,.06); padding: 0 .25rem; border-radius: 2px; }
  .counts { display: flex; flex-wrap: wrap; gap: .5rem; margin-bottom: 1.25rem; }
  .chip { border: 1px solid currentColor; border-radius: 999px;
          padding: .15rem .7rem; text-decoration: none; font-size: .85rem; }
  .chip.off { color: #57606a; }
  form { margin-bottom: 1rem; }
  input[type=search] { padding: .35rem .5rem; border: 1px solid #d0d7de;
                       border-radius: 4px; min-width: 16rem; font: inherit; }
  table { border-collapse: collapse; width: 100%; }
  th, td { text-align: left; padding: .45rem .6rem;
           border-bottom: 1px solid #d0d7de; vertical-align: top; }
  th { font-size: .75rem; text-transform: uppercase; letter-spacing: .04em;
       color: #57606a; border-bottom-width: 2px; }
  td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .dot { display: inline-block; width: .6rem; height: .6rem;
         border-radius: 50%; margin-right: .4rem; }
  .health { white-space: nowrap; font-weight: 600; }
  .reason { color: #57606a; }
  .domain { font-weight: 600; }
  .url { color: #57606a; font-size: .8rem; word-break: break-all; }
  footer { margin-top: 2rem; color: #57606a; font-size: .85rem; }
</style>

<h1>Crawl status <span style="font-weight:400;color:#57606a">(demo page)</span></h1>
<p class="sub">
  <?= count($rows) ?> of <?= count($grid['agencies']) ?> agencies &middot;
  data from <strong><?= h($source) ?></strong> &middot;
  crawler last wrote
  <?= h($grid['summary']['updated_at'] ?? $grid['generated_at'] ?? 'unknown') ?>
</p>

<?php if ($source === 'fixture'): ?>
  <div class="banner">
    <strong>Rendering the bundled fixture.</strong>
    <?= $error
        ? 'The database was configured but unreachable: <code>' . h($error) . '</code>'
        : 'Set <code>SCRAPEV3_DB_HOST</code>, <code>SCRAPEV3_DB_USER</code> and '
          . '<code>SCRAPEV3_DB_PASSWORD</code> to read live rows.' ?>
    These are real rows from a crawl, shipped so the page renders before the
    database exists &mdash; but they do not update.
  </div>
<?php endif; ?>

<div class="counts">
  <a class="chip<?= empty($filters['health']) ? '' : ' off' ?>" href="?">all
    <?= count($grid['agencies']) ?></a>
  <?php foreach ($counts as $health => $n):
        $on = ($filters['health'] ?? null) === $health; ?>
    <a class="chip<?= $on ? '' : ' off' ?>" href="?health=<?= h($health) ?>"
       style="color:<?= demo_colour($bands[$health] ?? 'warn') ?>">
      <?= h($health) ?> <?= $n ?></a>
  <?php endforeach; ?>
</div>

<form method="get">
  <?php if (!empty($filters['health'])): ?>
    <input type="hidden" name="health" value="<?= h($filters['health']) ?>">
  <?php endif; ?>
  <input type="search" name="search" placeholder="filter by domain"
         value="<?= h($filters['search']) ?>">
</form>

<table>
  <thead>
    <tr>
      <th>a_id</th><th>Site</th><th>Health</th><th>Source</th>
      <th class="num">Articles</th><th class="num">Last 30d</th>
      <th>Last crawled</th><th>Why</th>
    </tr>
  </thead>
  <tbody>
  <?php foreach ($rows as $r): $colour = demo_colour($r['severity']); ?>
    <tr>
      <td class="num"><?= $r['a_id'] ?></td>
      <td>
        <div class="domain"><?= h($r['domain']) ?></div>
        <div class="url"><?= h($r['newsroom_url']) ?></div>
      </td>
      <td class="health" style="color:<?= $colour ?>">
        <span class="dot" style="background:<?= $colour ?>"></span><?= h($r['health']) ?>
      </td>
      <td><?= h($r['discovery_method'] ?? '—') ?></td>
      <td class="num"><?= $r['articles'] ?></td>
      <td class="num"><?= $r['articles_recent'] ?></td>
      <td><?= h(demo_ago($r['last_success_at'])) ?></td>
      <td class="reason"><?= h($r['reason']) ?></td>
    </tr>
  <?php endforeach; ?>
  <?php if (!$rows): ?>
    <tr><td colspan="8" class="reason">Nothing matches that filter.</td></tr>
  <?php endif; ?>
  </tbody>
</table>

<footer>
  Health is decided by the crawler and read from the table verbatim. To change
  what counts as healthy, edit <code>src/scrapev3/status.py</code> &mdash; not
  this page, and not the site.
</footer>
