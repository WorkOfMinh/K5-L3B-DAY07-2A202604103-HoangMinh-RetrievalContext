"""Snapshot integrity fixtures; vectors are synthetic and not benchmark results."""
from copy import deepcopy
import json
import unittest
from scripts.day6_snapshot import validate,object_hash


def fixture():
    rows=[{'strategy':'C','chunk_id':str(i),'embedding':json.dumps([1]+[0]*383)} for i in range(142)]
    manifest={'import_identity':{'db_digest':object_hash(rows)},'index_provenance':{'strategy':'C'},'model':{'signature':'fixture'}}
    snapshot={'strategy':'C','rows':rows,'database_digest':object_hash(rows),'index_provenance':{'strategy':'C'},'embedding_signature':'fixture'}
    snapshot['snapshot_sha256']=object_hash(snapshot)
    return snapshot,manifest


class Day6SnapshotTests(unittest.TestCase):
    def test_complete_snapshot_matches_recorded_digest(self):
        snapshot,manifest=fixture()
        self.assertIs(validate(snapshot,manifest),snapshot)

    def test_modified_vectors_or_provenance_are_rejected(self):
        snapshot,manifest=fixture();snapshot['rows'][0]['embedding']='[0]'
        with self.assertRaises(ValueError):validate(snapshot,manifest)
        snapshot,manifest=fixture();snapshot['index_provenance']={'strategy':'A'}
        snapshot['snapshot_sha256']=object_hash({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})
        with self.assertRaises(ValueError):validate(snapshot,manifest)

    def test_resealed_incomplete_shape_is_still_rejected(self):
        snapshot,manifest=fixture();snapshot['rows'][0]['embedding']='[1]'
        snapshot['database_digest']=object_hash(snapshot['rows'])
        manifest['import_identity']['db_digest']=snapshot['database_digest']
        snapshot['snapshot_sha256']=object_hash({k:v for k,v in snapshot.items() if k!='snapshot_sha256'})
        with self.assertRaises(ValueError):validate(snapshot,manifest)


if __name__=='__main__':unittest.main()
