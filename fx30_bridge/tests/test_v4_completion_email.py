import unittest

import bridge


def completed_status(destination_root="/Volumes/specialized2026/studio/Studio Recordings"):
    return {
        "localOnly": False,
        "delivery": {"provider": "LucidLink"},
        "postRecordSummary": {
            "state": "complete",
            "allChecksPassed": True,
            "jobId": "job-1",
        },
        "lastResult": {
            "state": "complete",
            "jobId": "job-1",
            "masters": [{
                "verified": True,
                "destination": f"{destination_root}/Just-In Masters/take.mov",
            }],
            "proxiesDelivered": [{
                "verified": True,
                "destination": f"{destination_root}/Just-In Proxies/take_PROXY.mp4",
            }],
        },
    }


class V4CompletionEmailTests(unittest.TestCase):
    def test_accepts_verified_lucidlink_receipts(self):
        job_id, delivered = bridge.v4_completion_email_items(completed_status())

        self.assertEqual(job_id, "job-1")
        self.assertEqual(len(delivered), 2)
        self.assertTrue(all(item["workflow"] == "studioos" for item in delivered))

    def test_rejects_local_receipts(self):
        self.assertIsNone(
            bridge.v4_completion_email_items(completed_status("/Users/podcaststudio/Documents"))
        )

    def test_studioos_email_uses_delivery_folder_route(self):
        _, delivered = bridge.v4_completion_email_items(completed_status())
        subject, body, html_body, _ = bridge.email_subject_and_body(
            {"studioUrl": "http://192.168.0.100:3030"}, delivered
        )

        self.assertIn("StudioOS recording ready", subject)
        self.assertIn("Just-In Masters", body)
        self.assertIn("https://app.lucidlink.com/", html_body)
        self.assertNotIn("192.168.0.100", html_body)
        self.assertIn("Open in LucidLink", html_body)
        self.assertIn("studio / Studio Recordings", html_body)


if __name__ == "__main__":
    unittest.main()