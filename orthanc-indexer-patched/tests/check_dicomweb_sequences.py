#!/usr/bin/env python3
"""Exercise a disposable Orthanc against synthetic DICOMs; never touches PACS data.

Run with the ssc-pacs Python environment:
  python orthanc-indexer-patched/tests/check_dicomweb_sequences.py --image IMAGE
Pass --expect-bug to demonstrate the failure against the pre-patch image.
"""
import argparse
import io
import json
import pathlib
import subprocess
import tempfile
import time
import uuid

import pydicom
import requests
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid


def fixture(study, comment):
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(None, {}, file_meta=meta, preamble=b'\0' * 128)
    ds.SOPClassUID = meta.MediaStorageSOPClassUID
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.PatientID = 'SYNTHETIC-QIDO-REGRESSION'
    ds.PatientName = 'SYNTHETIC^TEST'
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = generate_uid()
    ds.Modality = 'CT'
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.Rows = ds.Columns = 2
    ds.BitsAllocated = ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = 'MONOCHROME2'
    ds.PixelData = b'\x01\0\x02\0\x03\0\x04\0'
    item = Dataset()
    item.RequestedProcedureID = 'TEST-PROCEDURE'
    item.RequestedProcedureComments = comment
    nested = Dataset()
    nested.CodeValue = 'TEST-CODE'
    nested.CodingSchemeDesignator = '99TEST'
    item.RequestedProcedureCodeSequence = [nested]
    ds.RequestAttributesSequence = [item]
    out = io.BytesIO()
    pydicom.dcmwrite(out, ds, enforce_file_format=True)
    return ds, out.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--image', required=True)
    parser.add_argument('--expect-bug', action='store_true')
    args = parser.parse_args()
    name = 'ssc-qido-test-' + uuid.uuid4().hex[:12]
    with tempfile.TemporaryDirectory(prefix='ssc-qido-test-') as temp:
        config = pathlib.Path(temp) / 'orthanc.json'
        config.write_text(json.dumps({
            'StorageDirectory': '/tmp/orthanc-test',
            'Plugins': ['/usr/share/orthanc/plugins-available/libOrthancDicomWeb.so'],
            'AuthenticationEnabled': False, 'RemoteAccessAllowed': True,
            'DicomServerEnabled': False, 'HttpPort': 8042,
            'DicomWeb': {'Enable': True, 'Root': '/dicom-web/'},
        }))
        subprocess.run([
            'docker', 'run', '-d', '--name', name,
            '-p', '127.0.0.1::8042', '--entrypoint', 'Orthanc',
            '-v', f'{config}:/tmp/orthanc.json:ro', args.image, '/tmp/orthanc.json',
        ], check=True, stdout=subprocess.DEVNULL)
        try:
            port = subprocess.check_output(['docker', 'port', name, '8042'], text=True).strip()
            base = 'http://' + port
            session = requests.Session()
            session.trust_env = False
            for _ in range(100):
                try:
                    if session.get(base + '/system', timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                raise RuntimeError('Disposable Orthanc failed to start')
            for comment in ['short comment', '', 'long comment ' * 60]:
                study = generate_uid()
                ds, data = fixture(study, comment)
                response = session.post(base + '/instances', data=data, timeout=20)
                response.raise_for_status()
                oid = response.json()['ID']
                assert session.get(base + '/instances/' + oid + '/file').content == data
                response = session.get(base + '/dicom-web/studies/' + study + '/series')
                if args.expect_bug and len(comment) > 256:
                    assert response.status_code == 400, response.status_code
                    print('Reproduced HTTP 400 for a long nested comment')
                    continue
                response.raise_for_status()
                result = response.json()
                assert len(result) == 1
                assert result[0]['0020000E']['Value'] == [ds.SeriesInstanceUID]
                item = result[0]['00400275']['Value'][0]
                assert item['00401001']['Value'] == ['TEST-PROCEDURE']
                assert item['00321064']['Value'][0]['00080100']['Value'] == ['TEST-CODE']
                if len(comment) > 256:
                    assert not item['00401400'].get('Value')
                path = base + '/dicom-web/studies/' + study + '/series/' + ds.SeriesInstanceUID
                metadata = session.get(path + '/metadata').json()
                if comment:
                    assert metadata[0]['00400275']['Value'][0]['00401400']['Value'] == [comment.rstrip()]
                frames = session.get(path + '/instances/' + ds.SOPInstanceUID + '/frames/1',
                                     headers={'Accept': 'multipart/related; type=application/octet-stream; transfer-syntax=1.2.840.10008.1.2.1'})
                frames.raise_for_status()
                assert ds.PixelData in frames.content
                print(f'Passed nested comment length {len(comment)}: QIDO, metadata, original bytes, frame')
        except Exception:
            subprocess.run(['docker', 'logs', '--tail', '25', name], check=False)
            raise
        finally:
            subprocess.run(['docker', 'rm', '-f', name], check=False, stdout=subprocess.DEVNULL)


if __name__ == '__main__':
    main()
