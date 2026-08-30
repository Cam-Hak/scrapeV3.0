<?php
/**
 * Request a site for the scrapev3 crawl, from the website codebase.
 *
 * The mirror of remove_agency.php. Copy this file into the site; it needs
 * nothing from the crawler - no shared code, no file paths, no Python. The only
 * coupling is the table.
 *
 * The crawler seeds the request at the start of its next pass; it is not
 * listening, so this returns as soon as the row is committed.
 *
 * A removal outranks a request. If the agency is on removed_agency, the crawler
 * refuses this request every pass rather than resurrecting it - so a publisher
 * who asked to be taken out stays out even if a form here asks for them back.
 */

declare(strict_types=1);

/**
 * Connect to the shared state database.
 *
 * ERRMODE_EXCEPTION so a failed request is loud. A silently swallowed error
 * here means a site somebody asked for is never crawled and nobody finds out.
 */
function scrapev3RequestConnect(
    string $host,
    string $user,
    string $password,
    int $port = 3306,
    string $database = 'scrapev3'
): PDO {
    return new PDO(
        "mysql:host={$host};port={$port};dbname={$database};charset=utf8mb4",
        $user,
        $password,
        [
            PDO::ATTR_ERRMODE            => PDO::ERRMODE_EXCEPTION,
            PDO::ATTR_EMULATE_PREPARES   => false,
            PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        ]
    );
}

/**
 * Ask for a newsroom URL to be crawled.
 *
 * Idempotent: newsroom_url is the primary key, so submitting the same request
 * twice updates the note rather than failing. The caller never has to check
 * first. Re-requesting the same URL under a different a_id moves it - the
 * crawler holds one owner per newsroom, so correcting that has to be possible.
 *
 * Send the newsroom page - the index that lists press releases - not one
 * article and not the site's home page. Discovery starts from this URL and
 * works outward, so a home page makes it guess and an article gives it nothing
 * to guess from.
 *
 * Do not send a domain. The crawler derives the registrable domain itself,
 * because that value is its pacing and shard key: one supplied from outside
 * that disagreed would either split a publisher across two workers or hammer
 * it, and neither fails loudly.
 *
 * @param int         $aId the agency id - the same one in tns.agencies.
 * @param string      $newsroomUrl the page that lists the press releases.
 * @param string|null $note why, for the operator reading the list later.
 */
function scrapev3RequestSite(
    PDO $pdo,
    int $aId,
    string $newsroomUrl,
    ?string $note = null
): void {
    if ($aId <= 0) {
        throw new InvalidArgumentException("a_id must be a positive integer, got {$aId}");
    }
    $newsroomUrl = trim($newsroomUrl);
    // Checked here rather than left to the crawler because the person who can
    // fix a typo is the one submitting it, and they are standing right here. An
    // unusable URL that reaches the table is reported once a pass into a log
    // nobody is reading.
    if (!preg_match('~^https?://~i', $newsroomUrl)) {
        throw new InvalidArgumentException(
            "newsroom_url must be an http(s) URL, got '{$newsroomUrl}'"
        );
    }
    if (strlen($newsroomUrl) > 768) {
        throw new InvalidArgumentException('newsroom_url is longer than the column (768)');
    }

    $stmt = $pdo->prepare(
        'INSERT INTO requested_site (newsroom_url, a_id, requested_at, note)
         VALUES (:url, :a_id, UTC_TIMESTAMP(), :note)
         ON DUPLICATE KEY UPDATE a_id = VALUES(a_id), note = VALUES(note)'
    );
    $stmt->execute([':a_id' => $aId, ':url' => $newsroomUrl, ':note' => $note]);
}

/**
 * Sites already requested, newest first - for showing the operator what has
 * been submitted.
 *
 * Being on this list does not mean the site is being crawled. It means it has
 * been asked for; read agency_status to find out what happened.
 *
 * @return array<int, array{a_id:int, newsroom_url:string, requested_at:string, note:?string}>
 */
function scrapev3RequestedSites(PDO $pdo): array
{
    $stmt = $pdo->query(
        'SELECT a_id, newsroom_url, requested_at, note FROM requested_site
         ORDER BY requested_at DESC'
    );
    return $stmt->fetchAll();
}

/**
 * Has this newsroom URL already been requested for this agency?
 */
function scrapev3IsRequested(PDO $pdo, int $aId, string $newsroomUrl): bool
{
    $stmt = $pdo->prepare(
        'SELECT 1 FROM requested_site WHERE a_id = :a_id AND newsroom_url = :url'
    );
    $stmt->execute([':a_id' => $aId, ':url' => trim($newsroomUrl)]);
    return (bool) $stmt->fetchColumn();
}

// ---------------------------------------------------------------------------
// Example
// ---------------------------------------------------------------------------
//
// $pdo = scrapev3RequestConnect('10.0.0.5', 'website', getenv('SCRAPEV3_DB_PASSWORD'));
//
// try {
//     scrapev3RequestSite(
//         $pdo,
//         22385,
//         'https://example.org/about/news-releases',
//         'added by editorial, ticket #902'
//     );
// } catch (PDOException $e) {
//     // Do not tell the requester the site was added if this failed - they
//     // would stop watching for it.
//     error_log('scrapev3 site request failed: ' . $e->getMessage());
//     throw $e;
// }
//
// Nothing is crawled yet. The request is applied at the start of the crawler's
// next pass; on the crawler machine, `scrapev3 request --apply` does it now.
