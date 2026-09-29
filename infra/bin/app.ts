#!/usr/bin/env node
import * as cdk from 'aws-cdk-lib';
import { RlwaAppStack } from '../lib/app-stack.ts';
import { RlwaNetworkDbStack } from '../lib/network-db-stack.ts';

// Region is pinned (MANIFEST.md confirmed premises); `account` is
// deliberately left UNSET so both stacks stay account-agnostic. This is not
// a style choice — see RlwaNetworkDbStack's VPC comment: with `account`
// unresolved, `ec2.Vpc`'s AZ selection uses the lookup-free
// `Fn::GetAZs`/`Fn::Select` path instead of CDK's AWS-calling AZ context
// provider. Pinning both account and region would silently switch that
// lookup back on.
const env: cdk.Environment = { region: 'ap-northeast-1' };

const app = new cdk.App();

const networkDb = new RlwaNetworkDbStack(app, 'RlwaNetworkDbStack', {
  env,
  description: 'rlwa verification: VPC (public-only) + Aurora PostgreSQL Serverless v2 + secrets',
});

new RlwaAppStack(app, 'RlwaAppStack', {
  env,
  description: 'rlwa verification: Lambda functions (container / container+SnapStart / zip) x (web / api)',
  clusterEndpointHostname: networkDb.clusterEndpointHostname,
  dbSecretArn: networkDb.dbSecretArn,
  secretKeyBaseSecretArn: networkDb.secretKeyBaseSecretArn,
});
