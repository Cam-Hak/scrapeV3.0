<?php
/**
 * Remove an agency from the scrapev3 crawl, from the website codebase.
 *
 * Copy this file into the site. It needs nothing from the crawler - no shared
 * code, no file paths, no Python. The only coupling is the table.
 *
 * The crawler picks the removal up at the start of its next pass; it is not
 * listening, so this returns as soon as the row is committed.
 */

declare(strict_types=1);

/**
 * Connect to the shared state database.
 *
 * ERRMODE_EXCEPTION so a failed removal is loud. A silently swallowed error
 * here means a publisher who asked to be removed stays in the crawl.
 */
function scrapev3Connect(
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
 * Record an agency as removed.
 *
 * Idempotent: a_id is the primary key, so submitting the same removal twice
 * updates the note rather than failing. The caller never has to check first.
 *
 * @param int         $aId  the agency id - NOT a domain. One domain can carry
 *                          hundreds of agencies; house.gov carries 417.
 * @param string|null $note why, for the operator reading the list later.
 */
function scrapev3RemoveAgency(PDO $pdo, int $aId, ?string $note = null): void
{
    if ($aId <= 0) {
        throw new InvalidArgumentException("a_id must be a positive integer, got {$aId}");
    }

    $stmt = $pdo->prepare(
        'INSERT INTO removed_agency (a_id, removed_at, note)
         VALUES (:a_id, UTC_TIMESTAMP(), :note)
         ON DUPLICATE KEY UPDATE note = VALUES(note)'
    );
    $stmt->execute([':a_id' => $aId, ':note' => $note]);
}

/**
 * Agencies already removed, newest first - for showing the operator what has
 * been submitted.
 *
 * @return array<int, array{a_id:int, removed_at:string, note:?string}>
 */
function scrapev3RemovedAgencies(PDO $pdo): array
{
    $stmt = $pdo->query(
        'SELECT a_id, removed_at, note FROM removed_agency ORDER BY removed_at DESC'
    );
    return $stmt->fetchAll();
}

/**
 * Has this agency already been removed?
 */
function scrapev3IsRemoved(PDO $pdo, int $aId): bool
{
    $stmt = $pdo->prepare('SELECT 1 FROM removed_agency WHERE a_id = :a_id');
    $stmt->execute([':a_id' => $aId]);
    return (bool) $stmt->fetchColumn();
}

// ---------------------------------------------------------------------------
// Example
// ---------------------------------------------------------------------------
//
// $pdo = scrapev3Connect('10.0.0.5', 'website', getenv('SCRAPEV3_DB_PASSWORD'));
//
// try {
//     scrapev3RemoveAgency($pdo, 22385, 'requested by publisher, ticket #481');
// } catch (PDOException $e) {
//     // Do not report success to the requester if this failed - they would
//     // believe they had been removed when they had not.
//     error_log('scrapev3 removal failed: ' . $e->getMessage());
//     throw $e;
// }
