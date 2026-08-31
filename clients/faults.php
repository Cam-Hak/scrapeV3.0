<?php
/**
 * What is going wrong with the crawl, for the website to render.
 *
 * The third read-side table, and the same contract as `status.php`: every
 * function returns an array, nothing is emitted, columns are selected by name.
 *
 *     require 'faults.php';
 *     $worst = scrapev3_faults($pdo);              // ranked, worst first
 *     $mine  = scrapev3_faults($pdo, 'us');        // the to-do list
 *
 * `agency_status` answers "is this publisher being collected?" - one row per
 * agency, for a grid a publisher might see. This answers "what is wrong with
 * the crawler?" - one row per kind, across the whole corpus, for whoever
 * operates it. Do not put this on a publisher-facing page: `dns x20` is our
 * operational detail and means nothing to a newsroom.
 *
 * One row per kind, not per site. The domains that raised it are counted in
 * `domains` and one is named in `example_domain`; the full list lives on the
 * crawler (`scrapev3 faults --kind dns`), because a page that needed all of
 * them would be joining six hundred rows to print ten.
 *
 * The table is a SNAPSHOT of the last pass, rewritten each time and pruned of
 * anything that stopped happening. A kind fixed last week disappears rather
 * than lingering. History is kept on the crawler, not here.
 */

declare(strict_types=1);

/** The three bands, worst first. Closed, like severity in status.php. */
const SCRAPEV3_OWNERS = ['us', 'site', 'policy'];

/** Columns selected by name, so a column added upstream cannot shift a value. */
const SCRAPEV3_FAULT_COLUMNS =
    'kind, severity, owner, domains, occurrences, score, band, '
    . 'example_domain, sample_url, sample_detail, run_id, updated_at';

/**
 * Every fault kind from the last pass, worst first.
 *
 * Ranked by the crawler, not here: `score` is severity x how many domains
 * raised it x whose problem it is, and re-deriving that in PHP would be a
 * second definition of "worth fixing" that drifts from the crawler's. Order by
 * `score` and display `band`.
 *
 * `policy` rows - a robots.txt we obeyed, a bot wall - score 0 by construction
 * and sort to the bottom. They are returned so they can be counted, and they
 * are never the top of the list.
 *
 * @param string|null $owner 'us' | 'site' | 'policy', or null for all.
 * @return array<int, array<string, mixed>>
 */
function scrapev3_faults(PDO $pdo, ?string $owner = null, ?int $limit = null): array
{
    $sql = 'SELECT ' . SCRAPEV3_FAULT_COLUMNS . ' FROM crawl_fault';
    $params = [];
    if ($owner !== null) {
        if (!in_array($owner, SCRAPEV3_OWNERS, true)) {
            throw new InvalidArgumentException("not an owner: $owner");
        }
        $sql .= ' WHERE owner = :owner';
        $params['owner'] = $owner;
    }
    // `kind` breaks the tie so paging cannot repeat or skip a row - the same
    // total-order rule the status grid needs.
    $sql .= ' ORDER BY score DESC, occurrences DESC, kind';
    if ($limit !== null) {
        $sql .= ' LIMIT ' . max(1, $limit);
    }

    $stmt = $pdo->prepare($sql);
    $stmt->execute($params);
    return array_map('scrapev3_cast_fault', $stmt->fetchAll());
}

/**
 * Counts per owner, plus how fresh the tracker is.
 *
 * Show `updated_at`. This table is written by a batch job that can simply stop
 * running, and a tracker frozen at last Tuesday looks exactly like a quiet
 * week - the same trap as the status grid.
 *
 * @return array{total:int, kinds:int, owner:array<string,int>, updated_at:?string, run_id:?string}
 */
function scrapev3_fault_summary(PDO $pdo): array
{
    $stmt = $pdo->query(
        'SELECT owner, COUNT(*) AS kinds, SUM(occurrences) AS n, '
        . 'MAX(updated_at) AS updated_at, MAX(run_id) AS run_id '
        . 'FROM crawl_fault GROUP BY owner'
    );

    $out = ['total' => 0, 'kinds' => 0,
            'owner' => ['us' => 0, 'site' => 0, 'policy' => 0],
            'updated_at' => null, 'run_id' => null];
    foreach ($stmt->fetchAll() as $row) {
        $out['total'] += (int) $row['n'];
        $out['kinds'] += (int) $row['kinds'];
        $out['owner'][$row['owner']] = (int) $row['n'];
        $out['updated_at'] = max($out['updated_at'], $row['updated_at']);
        $out['run_id'] = max($out['run_id'], $row['run_id']);
    }
    return $out;
}

/**
 * One kind, or null if it did not occur on the last pass.
 *
 * Null is a real answer and a good one: the kind is not currently happening.
 */
function scrapev3_fault(PDO $pdo, string $kind): ?array
{
    $stmt = $pdo->prepare('SELECT ' . SCRAPEV3_FAULT_COLUMNS
                          . ' FROM crawl_fault WHERE kind = :kind');
    $stmt->execute(['kind' => $kind]);
    $row = $stmt->fetch();
    return $row === false ? null : scrapev3_cast_fault($row);
}

/**
 * Ranked list plus the counts above it, in one call.
 *
 * The shape `scrapev3 faults --json` writes, so a page built against a fixture
 * keeps working when it is pointed at the database.
 */
function scrapev3_fault_grid(PDO $pdo, ?string $owner = null, ?int $limit = null): array
{
    return [
        'generated_at' => gmdate('Y-m-d H:i:s'),
        'summary'      => scrapev3_fault_summary($pdo),
        'faults'       => scrapev3_faults($pdo, $owner, $limit),
    ];
}

/**
 * MySQL hands every column back as a string. Cast once, here, so callers can
 * compare numbers with `>` and sort without surprises.
 */
function scrapev3_cast_fault(array $row): array
{
    foreach (['severity', 'domains', 'occurrences'] as $key) {
        if (isset($row[$key])) {
            $row[$key] = (int) $row[$key];
        }
    }
    if (isset($row['score'])) {
        $row['score'] = (float) $row['score'];
    }
    return $row;
}

// ---------------------------------------------------------------------------
// Example
// ---------------------------------------------------------------------------
//
// require 'status.php';   // for scrapev3_connect
// require 'faults.php';
//
// $pdo = scrapev3_connect($host, 'website', getenv('SCRAPEV3_DB_PASSWORD'));
//
// foreach (scrapev3_faults($pdo, 'us', 10) as $f) {
//     printf("%-24s %s  %d domains  %s\n",
//            $f['kind'], $f['band'], $f['domains'], $f['sample_detail']);
// }
//
// And per agency, from the grid you already read - no second query:
//
// $row = scrapev3_status($pdo, 22385);
// if ($row['fault_kind']) {
//     echo "last failure: {$row['fault_kind']} - {$row['fault_detail']}";
// }
