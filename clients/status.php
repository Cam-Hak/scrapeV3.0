<?php
/**
 * Read crawl health from the scrapev3 status table.
 *
 * DATA ONLY. Nothing here echoes, prints, or emits HTML - every function
 * returns a PHP array, so the site renders it however it likes. Including this
 * file must produce no output whatsoever; anything that does would corrupt a
 * JSON response or a header on whatever page pulls it in.
 *
 * The crawler owns the table and refreshes it at the end of each pass. Reading
 * is a plain SELECT against a snapshot - there is no request to the crawler,
 * nothing to wait on, and no way for a page load here to slow a crawl down. A
 * refresh button on the site is just this query run again.
 *
 * Health is decided by the crawler, not here. A row already carries the verdict
 * (`health`), a colour band (`severity`), and a sentence (`reason`), because
 * duplicating those rules in a template is how the dashboard ends up disagreeing
 * with the crawler about which sites are working.
 */

declare(strict_types=1);

/**
 * The colour bands, in the order a grid should sort them: worst first.
 *
 * `severity` is a closed three-value vocabulary and is the only field worth
 * switching on. `health` is open - the crawler may learn new words for new
 * faults - so treat it as a label to display, never as a condition to branch on.
 */
const SCRAPEV3_SEVERITIES = ['error', 'warn', 'ok'];

/**
 * Open a read-only connection to the crawler's state database.
 *
 * Exceptions, not warnings: a dashboard that silently renders an empty grid
 * when the database is unreachable is worse than one that shows an error, since
 * "no sites are failing" and "we cannot tell" look identical.
 */
function scrapev3_connect(
    string $host,
    string $user,
    string $password,
    int $port = 3306,
    string $database = 'scrapev3'
): PDO {
    $dsn = sprintf('mysql:host=%s;port=%d;dbname=%s;charset=utf8mb4',
                   $host, $port, $database);
    return new PDO($dsn, $user, $password, [
        PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        // Real prepared statements, so a filter value can never be parsed as
        // SQL no matter where the caller got it from.
        PDO::ATTR_EMULATE_PREPARES   => false,
    ]);
}

/** Columns selected by name, so a column added upstream cannot shift a value. */
const SCRAPEV3_STATUS_COLUMNS =
    'a_id, domain, newsroom_url, enabled, health, severity, reason, '
    . 'discovery_method, targets, consec_failures, needs_browser, articles, '
    . 'articles_recent, median_body_len, last_success_at, last_article_at, '
    // The inventory half: not the verdict on the agency, but the plan for it -
    // whether discovery is solved, when we last actually pulled a document, and
    // when the schedule comes back.
    . 'targets_cached, feed_url, feed_absent, probed_at, conditional_get, '
    . 'next_due_at, crawl_delay_s, revisit_period_s, first_stored_at, '
    . 'last_stored_at, tns_loaded, tns_pending, '
    . 'updated_at';

/** The same names as an array, for checking a caller's sort key against. */
const SCRAPEV3_SORTABLE = [
    'a_id', 'domain', 'newsroom_url', 'enabled', 'health', 'severity', 'reason',
    'discovery_method', 'targets', 'consec_failures', 'needs_browser',
    'articles', 'articles_recent', 'median_body_len', 'last_success_at',
    'last_article_at', 'targets_cached', 'feed_url', 'feed_absent', 'probed_at',
    'conditional_get', 'next_due_at', 'crawl_delay_s', 'revisit_period_s',
    'first_stored_at', 'last_stored_at', 'tns_loaded', 'tns_pending',
    'updated_at',
];

/** Worst first, and the default when nobody asks for anything else. */
const SCRAPEV3_RANK  = "FIELD(severity, 'error', 'warn', 'ok')";
const SCRAPEV3_ORDER = 'ORDER BY ' . SCRAPEV3_RANK . ', articles DESC, a_id';

/**
 * The ORDER BY clause for a sort key, or the default worst-first one.
 *
 * Three things it guarantees, all of which matter on real data:
 *
 * - **Nulls last in both directions.** MySQL puts them first ascending, and
 *   "never pulled a document" is not the smallest date - it is the absence of
 *   one, and it belongs at the bottom either way.
 * - **A total order.** `a_id` breaks every tie, always ascending. Sorting by
 *   `health` leaves two thousand rows tied, and without a tiebreak the same
 *   page-2 query can return rows that were already on page 1.
 * - **`severity` sorts by rank, not alphabetically** - alphabetical reads
 *   "error, ok, warn", the one order in which broken sites are not first.
 *
 * The key is checked against SCRAPEV3_SORTABLE and never interpolated from
 * what a caller sent. A query string is the usual source of a sort key, so
 * this is the one place user input gets near the SQL.
 */
function scrapev3_order_by(?string $sort = null, bool $desc = false): string
{
    if ($sort === null) {
        return SCRAPEV3_ORDER;
    }
    if (!in_array($sort, SCRAPEV3_SORTABLE, true)) {
        throw new InvalidArgumentException("not a sortable column: $sort");
    }
    $key = $sort === 'severity' ? SCRAPEV3_RANK : $sort;
    return "ORDER BY ($sort IS NULL), $key " . ($desc ? 'DESC' : 'ASC') . ', a_id';
}

/**
 * Compare text the way the database does, not the way PHP does.
 *
 * `agency_status` is utf8mb4_0900_ai_ci, so MySQL treats "A" and "a" as the
 * same letter. PHP's comparison operators do not, so the two orderings diverge
 * the moment a value carries a capital. 250 of the newsroom URLs do
 * (`navy.mil/Press-Office`, `centcom.mil/MEDIA`), and the live rows and the
 * fixture rows would come back in different orders on the same page,
 * intermittently, depending on which hosts happened to collide.
 *
 * Case is folded; accents are not. `ai` also equates "e" and "é", which would
 * need full Unicode collation to reproduce - out of proportion for columns
 * holding hostnames, URLs and English sentences.
 */
function scrapev3_sort_key(array $row, string $sort)
{
    if ($sort === 'severity') {
        $rank = array_flip(SCRAPEV3_SEVERITIES);
        return $rank[$row['severity']] ?? 9;
    }
    $value = $row[$sort] ?? null;
    return is_string($value) ? strtolower($value) : $value;
}

/**
 * The same order as scrapev3_order_by(), applied in PHP.
 *
 * For the no-database path: rows read from a JSON fixture have not been
 * through MySQL and still have to come out in the order the site would get
 * live. Two orderings that differ only sometimes is worse than either one.
 */
function scrapev3_sort_rows(array $rows, ?string $sort = null, bool $desc = false): array
{
    if ($sort !== null && !in_array($sort, SCRAPEV3_SORTABLE, true)) {
        throw new InvalidArgumentException("not a sortable column: $sort");
    }

    if ($sort === null) {
        usort($rows, static function (array $a, array $b): int {
            $rank = array_flip(SCRAPEV3_SEVERITIES);
            return [$rank[$a['severity']] ?? 9, -$a['articles'], $a['a_id']]
               <=> [$rank[$b['severity']] ?? 9, -$b['articles'], $b['a_id']];
        });
        return $rows;
    }

    // Nulls out of the way first, so the comparator never has to rank one
    // against a real value - and so they stay last when $desc flips the rest.
    $present = array_values(array_filter($rows,
        static fn(array $r): bool => ($r[$sort] ?? null) !== null));
    $absent  = array_values(array_filter($rows,
        static fn(array $r): bool => ($r[$sort] ?? null) === null));

    // The a_id tiebreak is written out rather than left to sort stability,
    // because it is the tiebreak MySQL applies and it must not flip with $desc.
    usort($present, static function (array $a, array $b) use ($sort, $desc): int {
        $cmp = scrapev3_sort_key($a, $sort) <=> scrapev3_sort_key($b, $sort);
        if ($cmp !== 0) {
            return $desc ? -$cmp : $cmp;
        }
        return $a['a_id'] <=> $b['a_id'];
    });
    usort($absent, static fn(array $a, array $b): int => $a['a_id'] <=> $b['a_id']);

    return array_merge($present, $absent);
}

/**
 * Every agency's status, worst first.
 *
 * @param array $filters Optional: 'severity', 'health', 'domain', 'search',
 *                        'uncached' (truthy), 'due' (truthy),
 *                        'sort' (a column name), 'desc' (truthy).
 * @return array<int, array<string, mixed>> One row per agency.
 */
function scrapev3_statuses(PDO $pdo, array $filters = [], ?int $limit = null): array
{
    $where  = [];
    $params = [];

    foreach (['severity', 'health', 'domain'] as $key) {
        if (!empty($filters[$key])) {
            $where[]       = "$key = :$key";
            $params[$key]  = $filters[$key];
        }
    }
    if (!empty($filters['search'])) {
        $where[]          = '(domain LIKE :search OR newsroom_url LIKE :search)';
        $params['search'] = '%' . $filters['search'] . '%';
    }
    // Not a health word, and deliberately not derived from one: an agency can
    // be perfectly healthy and still own a newsroom the cascade has never
    // solved. That is the list worth looking at when deciding what needs work.
    if (!empty($filters['uncached'])) {
        $where[] = 'targets_cached < targets';
    }
    if (!empty($filters['due'])) {
        $where[] = 'enabled = 1 AND (next_due_at IS NULL '
                 . 'OR next_due_at <= UTC_TIMESTAMP())';
    }

    // FIELD() puts errors at the top rather than sorting the bands
    // alphabetically, which would read "error, ok, warn" - the one order in
    // which the broken sites are not first.
    $sql = 'SELECT ' . SCRAPEV3_STATUS_COLUMNS . ' FROM agency_status'
         . ($where ? ' WHERE ' . implode(' AND ', $where) : '')
         . ' ' . scrapev3_order_by($filters['sort'] ?? null,
                                   !empty($filters['desc']));
    if ($limit !== null) {
        $sql .= ' LIMIT ' . max(1, $limit);
    }

    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    return array_map('scrapev3_cast_row', $stmt->fetchAll());
}

/**
 * One agency, or null if the crawler does not hold it.
 *
 * Null is a real answer and means something specific: the agency is not in the
 * frontier at all - never seeded, or removed on request. It is not the same as
 * an agency with nothing to report, which returns a row saying so.
 */
function scrapev3_status(PDO $pdo, int $a_id): ?array
{
    $stmt = $pdo->prepare('SELECT ' . SCRAPEV3_STATUS_COLUMNS
                          . ' FROM agency_status WHERE a_id = :a_id');
    $stmt->execute(['a_id' => $a_id]);
    $row = $stmt->fetch();
    return $row === false ? null : scrapev3_cast_row($row);
}

/**
 * Several agencies at once, keyed by a_id.
 *
 * For a page that already knows which agencies it is showing: one query for the
 * whole list instead of a call per row.
 *
 * @param int[] $a_ids
 * @return array<int, array<string, mixed>>
 */
function scrapev3_statuses_for(PDO $pdo, array $a_ids): array
{
    $a_ids = array_values(array_unique(array_map('intval', $a_ids)));
    if (!$a_ids) {
        return [];
    }
    $marks = implode(',', array_fill(0, count($a_ids), '?'));
    $stmt  = $pdo->prepare('SELECT ' . SCRAPEV3_STATUS_COLUMNS
                           . " FROM agency_status WHERE a_id IN ($marks)");
    $stmt->execute($a_ids);

    $out = [];
    foreach ($stmt->fetchAll() as $row) {
        $row = scrapev3_cast_row($row);
        $out[$row['a_id']] = $row;
    }
    return $out;
}

/**
 * Counts per health word and per severity, plus how fresh the grid is.
 *
 * `updated_at` is the crawler's last write. Show it: a grid with no way to tell
 * live numbers from a frozen snapshot is the failure mode of every dashboard,
 * and this one is refreshed by a batch job that can simply stop running.
 */
function scrapev3_summary(PDO $pdo): array
{
    $rows = $pdo->query(
        'SELECT health, severity, COUNT(*) AS n, MAX(updated_at) AS updated_at '
        . 'FROM agency_status GROUP BY health, severity'
    )->fetchAll();

    $summary = ['total' => 0, 'health' => [], 'severity' => [], 'updated_at' => null];
    foreach (SCRAPEV3_SEVERITIES as $band) {
        $summary['severity'][$band] = 0;
    }
    foreach ($rows as $row) {
        $n = (int) $row['n'];
        $summary['total']                     += $n;
        $summary['health'][$row['health']]     =
            ($summary['health'][$row['health']] ?? 0) + $n;
        $summary['severity'][$row['severity']] =
            ($summary['severity'][$row['severity']] ?? 0) + $n;
        if ($summary['updated_at'] === null
            || $row['updated_at'] > $summary['updated_at']) {
            $summary['updated_at'] = $row['updated_at'];
        }
    }
    arsort($summary['health']);
    return $summary;
}

/**
 * Everything a grid needs, in one call: the rows and the counts above them.
 *
 * The shape is identical to `scrapev3 status --json`, so a page written against
 * the fixture keeps working when it is pointed at the database.
 */
function scrapev3_grid(PDO $pdo, array $filters = [], ?int $limit = null): array
{
    return [
        'generated_at' => gmdate('Y-m-d H:i:s'),
        'summary'      => scrapev3_summary($pdo),
        'agencies'     => scrapev3_statuses($pdo, $filters, $limit),
    ];
}

/**
 * MySQL hands back every column as a string. Cast once, here, so callers can
 * compare numbers with `>` and test flags with `if` and be right.
 */
function scrapev3_cast_row(array $row): array
{
    foreach (['a_id', 'targets', 'consec_failures', 'articles',
              'articles_recent', 'targets_cached', 'revisit_period_s',
              'tns_loaded', 'tns_pending'] as $key) {
        if (isset($row[$key])) {
            $row[$key] = (int) $row[$key];
        }
    }
    foreach (['enabled', 'needs_browser', 'feed_absent',
              'conditional_get'] as $key) {
        if (isset($row[$key])) {
            $row[$key] = (bool) (int) $row[$key];
        }
    }
    if (isset($row['crawl_delay_s'])) {
        $row['crawl_delay_s'] = (float) $row['crawl_delay_s'];
    }
    // Nullable: absent is meaningful (never measured), and 0 is not the same.
    $row['median_body_len'] = isset($row['median_body_len'])
        ? (int) $row['median_body_len'] : null;
    return $row;
}

/**
 * Read the same payload from a JSON file instead of the database.
 *
 * For `status_demo.php`, and for a staging site with no database access: the
 * crawler writes this file with `scrapev3 status --json <path>`, and the shape
 * is the same as `scrapev3_grid`, so nothing that consumes it has to care.
 */
function scrapev3_grid_from_file(string $path, ?string $sort = null,
                                 bool $desc = false): array
{
    $raw = @file_get_contents($path);
    if ($raw === false) {
        throw new RuntimeException("Cannot read status fixture: $path");
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || !isset($data['agencies'])) {
        throw new RuntimeException("Not a scrapev3 status payload: $path");
    }
    $data['agencies'] = scrapev3_sort_rows(
        array_map('scrapev3_cast_row', $data['agencies']), $sort, $desc);
    return $data;
}
