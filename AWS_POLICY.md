# AWS policy

To be able to deploy the desired resources, you must create a new custom role and attribute it to your AWS user.

- Suggested username: `repo-autodeployer-demo`
- Suggested policy name: `plc-repo-autodeployer`

## Policy

```json
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Action": "ec2:*",
            "Resource": "*"
        },
        {
            "Sid": "EC2KeyPairManagement",
            "Effect": "Allow",
            "Action": [
                "ec2:ImportKeyPair"
            ],
            "Resource": "*"
        },
        {
            "Sid": "EC2InstanceManagement",
            "Effect": "Allow",
            "Action": [
                "ec2:RunInstances",
                "ec2:TerminateInstances",
                "ec2:DescribeInstances",
                "ec2:DescribeImages",
                "ec2:DescribeVpcs",
                "ec2:CreateTags",
                "ec2:DescribeInstanceTypes"
            ],
            "Resource": "*"
        },
        {
            "Sid": "EC2SecurityGroupManagement",
            "Effect": "Allow",
            "Action": [
                "ec2:CreateSecurityGroup",
                "ec2:AuthorizeSecurityGroupIngress",
                "ec2:AuthorizeSecurityGroupEgress",
                "ec2:RevokeSecurityGroupEgress",
                "ec2:DeleteSecurityGroup",
                "ec2:DescribeSecurityGroups",
                "ec2:CreateTags"
            ],
            "Resource": "*"
        },
        {
            "Sid": "EC2DescribeTags",
            "Effect": "Allow",
            "Action": "ec2:DescribeTags",
            "Resource": "*"
        }
    ]
}
```
