<?php
/**
 * TESTING PAGE - NOT FOR PRODUCTION.
 *
 * A worked example of rendering `status.php`, so the data can be looked at
 * before any of it is wired into the real site.
 *
 * It does two things: get the rows, and hand them to the page. It calls nothing
 * but `status.php` for the first, and it does not implement the second at all -
 * the page is `src/scrapev3/status_view.html`, the same file
 * `scrapev3 status --html` renders. There is deliberately no markup here, no
 * CSS, no severity-to-colour table and no column list. An earlier version of
 * this file had all four, alongside a second copy in Python, and the two
 * disagreed about which columns existed within a day of being written.
 *
 * So the fetch is the only thing that varies. PDO here, pymysql in the crawler,
 * a JSON file in either - same payload, same page.
 *
 * It runs with no database. With none configured it renders the fixture written
 * by `scrapev3 status --json data/status.json`, through
 * `scrapev3_grid_from_file` - still a real client function, the same one a
 * staging site with no database access uses.
 *
 *     scrapev3 status --json data/status.json
 *     php -S localhost:8000 -t clients      # then open /status_demo.php
 *
 * What to copy into the real site: `status.php`, and the habit of letting the
 * crawler decide what `health` means. What NOT to copy: this file's fallback to
 * a fixture, and its habit of catching every exception and carrying on.
 */

declare(strict_types=1);

require __DIR__ . '/status.php';

// Outside `clients/`, because it is generated output rather than something the
// repository ships. The committed fixture that used to live here was deleted
// for that reason and should not come back.
const DEMO_FIXTURE = __DIR__ . '/../data/status.json';

// Package data, not a client file. It is read from the source tree, which is
// why this page is only for running out of the repository - a copy of
// `clients/` on its own web server would not find it, and is not meant to.
const DEMO_VIEW = __DIR__ . '/../src/scrapev3/status_view.html';

/**
 * Live rows when a database is configured, the fixture otherwise.
 *
 * The source is reported back to the page rather than hidden, because a demo
 * that silently falls back to canned data is a demo that will eventually be
 * mistaken for a working dashboard.
 *
 * No filters and no limit: filtering is the page's job now, and doing it here
 * as well is how the old version ended up with one implementation for fixture
 * rows and another for live ones. A real site passes filters to
 * `scrapev3_statuses()` and renders however it likes.
 *
 * @return array{0: array, 1: string} the grid, and the line describing it.
 */
function demo_load(): array
{
    $host = getenv('SCRAPEV3_DB_HOST');
    if (!$host) {
        return [
            scrapev3_grid_from_file(DEMO_FIXTURE),
            'Rendering the fixture at ' . DEMO_FIXTURE . '. These are real rows'
            . ' from a crawl, but they do not update on their own. Set'
            . ' SCRAPEV3_DB_HOST, SCRAPEV3_DB_USER and SCRAPEV3_DB_PASSWORD to'
            . ' read live rows.',
        ];
    }
    try {
        $pdo = scrapev3_connect(
            $host,
            (string) (getenv('SCRAPEV3_DB_USER') ?: 'website'),
            (string) (getenv('SCRAPEV3_DB_PASSWORD') ?: '')
        );
        return [scrapev3_grid($pdo), ''];
    } catch (Throwable $e) {
        // Fall back so the page still renders, but say so loudly: an empty grid
        // and a healthy grid must never look the same.
        return [
            scrapev3_grid_from_file(DEMO_FIXTURE),
            'The database was configured but unreachable (' . $e->getMessage()
            . '), so these are stale rows from ' . DEMO_FIXTURE . '.',
        ];
    }
}

[$grid, $note] = demo_load();

$view = @file_get_contents(DEMO_VIEW);
if ($view === false) {
    http_response_code(500);
    exit('Cannot read ' . DEMO_VIEW . " - run this page from the repository.\n");
}

// json_encode already escapes forward slashes, so `</script>` cannot appear -
// but the crawler escapes it explicitly and so does this, because the one thing
// that must not differ between the two producers is the payload.
$payload = str_replace('</', '<\\/', json_encode($grid));

header('Content-Type: text/html; charset=utf-8');
echo str_replace(
    ['__DATA__', '__NOTE__'],
    [$payload, str_replace('</', '<\\/', $note)],
    $view
);
