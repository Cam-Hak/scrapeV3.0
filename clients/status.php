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
    . 'updated_at';

/**
 * Every agency's status, worst first.
 *
 * @param array $filters Optional: 'severity', 'health', 'domain', 'search'.
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

    // FIELD() puts errors at the top rather than sorting the bands
    // alphabetically, which would read "error, ok, warn" - the one order in
    // which the broken sites are not first.
    $sql = 'SELECT ' . SCRAPEV3_STATUS_COLUMNS . ' FROM agency_status'
         . ($where ? ' WHERE ' . implode(' AND ', $where) : '')
         . " ORDER BY FIELD(severity, 'error', 'warn', 'ok'), articles DESC, a_id";
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
              'articles_recent'] as $key) {
        if (isset($row[$key])) {
            $row[$key] = (int) $row[$key];
        }
    }
    foreach (['enabled', 'needs_browser'] as $key) {
        if (isset($row[$key])) {
            $row[$key] = (bool) (int) $row[$key];
        }
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
function scrapev3_grid_from_file(string $path): array
{
    $raw = @file_get_contents($path);
    if ($raw === false) {
        throw new RuntimeException("Cannot read status fixture: $path");
    }
    $data = json_decode($raw, true);
    if (!is_array($data) || !isset($data['agencies'])) {
        throw new RuntimeException("Not a scrapev3 status payload: $path");
    }
    $data['agencies'] = array_map('scrapev3_cast_row', $data['agencies']);
    usort($data['agencies'], static function (array $a, array $b): int {
        $rank = array_flip(SCRAPEV3_SEVERITIES);
        return [$rank[$a['severity']] ?? 9, -$a['articles'], $a['a_id']]
           <=> [$rank[$b['severity']] ?? 9, -$b['articles'], $b['a_id']];
    });
    return $data;
}
