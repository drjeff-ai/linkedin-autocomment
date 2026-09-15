"""Standalone CLI runner that chains the scrape → generate → post steps.

A non-dashboard entry point for running the pipeline from the command line;
delegates to the same per-step package modules and ``linkedin_automation``.
"""

import subprocess
import sys
import os
import json
import argparse
import logging
import time
from datetime import datetime
from pathlib import Path

# Make `import linkedin_automation` resolve when run as `python tools/run_linkedin_workflow.py`.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from linkedin_automation import profile_manager as pm
from linkedin_automation import post_store

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class LinkedInWorkflowRunner:
    """Orchestrates the full LinkedIn engagement workflow"""
    
    def __init__(self, args):
        self.max_posts = args.max_posts
        self.min_quality = args.min_quality
        self.debug = args.debug
        self.comment_model = args.model
        self.comment_limit = args.comment_limit
        self.skip_comments = args.skip_comments
        self.dual_run = args.dual_run
        self.profile = args.profile  # Profile name for multi-account support
        
        # Resolve profile-specific data directories
        resolved_name = args.profile or pm.get_default_profile_name() or "default"
        self.timeline_dir = pm.get_timeline_dir(resolved_name)
        self.comments_dir = pm.get_comments_dir(resolved_name)
        
        # Steps run as package modules (python -m ...), so there is no script
        # file to check for; import errors surface at subprocess launch instead.
        self.finder_module = "linkedin_automation.post_finder"
        self.comment_module = "linkedin_automation.comment_generator"

        # This workflow is LinkedIn's: post_finder scrapes the LinkedIn feed and
        # the poster posts LinkedIn comments. The platform is therefore a fact
        # about this runner, stated once here and passed explicitly downstream —
        # not left for the generator to assume.
        self.platform = post_store.LINKEDIN
    
    def run_post_finder(self, run_number=1) -> str:
        """Run the post finder script and return the output file path"""
        if run_number > 1:
            logger.info("\n" + "="*60)
            logger.info(f"RUN #{run_number}: Finding MORE AI Discussion Posts")
            logger.info("="*60)
        else:
            logger.info("="*60)
            logger.info("STEP 1: Finding AI Discussion Posts on LinkedIn")
            logger.info("="*60)
        
        cmd = [
            sys.executable, "-m",
            self.finder_module,
            "--max-posts", str(self.max_posts),
            "--min-quality", str(self.min_quality)
        ]
        
        if self.debug:
            cmd.append("--debug")
        
        if self.profile:
            cmd.extend(["--profile", self.profile])
        
        logger.info(f"Running: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        # Print the output
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        
        # Check if output file was created
        output_file = self._extract_output_file(result.stdout)
        
        if not output_file:
            output_file = self._extract_output_file("")
        
        if output_file and os.path.exists(output_file):
            logger.info(f"✓ Post finder run #{run_number} completed successfully")
            logger.info(f"✓ Output file: {output_file}")
            return output_file
        else:
            raise RuntimeError(f"Could not find output file from post finder run #{run_number}")
    
    def _extract_output_file(self, output: str) -> str:
        """Extract the output file path from the script output"""
        if not os.path.exists(self.timeline_dir):
            return None
        
        json_files = list(Path(self.timeline_dir).glob("ai_posts_*.json"))
        
        if not json_files:
            return None
        
        latest_file = max(json_files, key=os.path.getctime)
        return str(latest_file)
    
    def merge_post_files(self, file1: str, file2: str) -> str:
        """Merge two post files, removing duplicates"""
        logger.info("\n" + "="*60)
        logger.info("MERGING POST FILES")
        logger.info("="*60)
        
        # Load both files
        with open(file1, 'r', encoding='utf-8') as f:
            data1 = json.load(f)
        
        with open(file2, 'r', encoding='utf-8') as f:
            data2 = json.load(f)
        
        # Merge quality posts (deduplicate by URL)
        seen_urls = set()
        merged_quality_posts = []
        
        for post in data1.get('quality_posts', []) + data2.get('quality_posts', []):
            url = post.get('url')
            if url and url not in seen_urls:
                seen_urls.add(url)
                merged_quality_posts.append(post)
            elif not url:
                # If no URL, use activity_urn or text hash for dedup
                identifier = post.get('activity_urn') or post.get('text', '')[:100]
                if identifier not in seen_urls:
                    seen_urls.add(identifier)
                    merged_quality_posts.append(post)
        
        # Sort by relevance score
        merged_quality_posts.sort(key=lambda x: x.get('relevance_score', 0), reverse=True)
        
        # Merge all posts (same dedup logic)
        seen_urls_all = set()
        merged_all_posts = []
        
        for post in data1.get('all_posts', []) + data2.get('all_posts', []):
            url = post.get('url')
            if url and url not in seen_urls_all:
                seen_urls_all.add(url)
                merged_all_posts.append(post)
            elif not url:
                identifier = post.get('activity_urn') or post.get('text', '')[:100]
                if identifier not in seen_urls_all:
                    seen_urls_all.add(identifier)
                    merged_all_posts.append(post)
        
        # Calculate post type distribution
        post_type_distribution = {}
        for post in merged_all_posts:
            post_type = post.get('post_type', 'unknown')
            post_type_distribution[post_type] = post_type_distribution.get(post_type, 0) + 1
        
        # Create merged output
        merged_data = {
            'scan_date': datetime.now().isoformat(),
            'merged_from': [file1, file2],
            'total_scanned': len(merged_all_posts),
            'quality_found': len(merged_quality_posts),
            'post_type_distribution': post_type_distribution,
            'quality_posts': merged_quality_posts,
            'all_posts': merged_all_posts
        }
        
        # Save merged file
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        merged_file = os.path.join(self.timeline_dir, f'ai_posts_merged_{timestamp}.json')
        
        with open(merged_file, 'w', encoding='utf-8') as f:
            json.dump(merged_data, f, indent=2, ensure_ascii=False)
        
        logger.info("✓ Merged files successfully")
        logger.info(f"  File 1: {data1.get('quality_found', 0)} quality posts")
        logger.info(f"  File 2: {data2.get('quality_found', 0)} quality posts")
        logger.info(f"  Merged: {len(merged_quality_posts)} unique quality posts")
        logger.info(f"  Total unique posts: {len(merged_all_posts)}")
        logger.info(f"✓ Merged file: {merged_file}")
        
        return merged_file
    
    def run_comment_generator(self, input_file: str):
        """Run the comment generator script"""
        logger.info("\n" + "="*60)
        logger.info("STEP 2: Generating Authentic Comments")
        logger.info("="*60)
        
        cmd = [
            sys.executable, "-m",
            self.comment_module,
            input_file,
            "--model", self.comment_model,
            "--limit", str(self.comment_limit),
            # Named, never defaulted. This workflow drives LinkedIn end to end
            # (post_finder is LinkedIn's scraper), so the platform is a property
            # of the workflow, not something to infer at the far end.
            "--platform", self.platform,
        ]
        
        if self.profile:
            cmd.extend(["--profile", self.profile])
        
        logger.info(f"Running: {' '.join(cmd)}")
        
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding='utf-8',
            errors='replace'
        )
        
        # Print the output
        print(result.stdout)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        
        # Check if comment files were created (profile-specific dir)
        if os.path.exists(self.comments_dir):
            comment_files = list(Path(self.comments_dir).glob("comments_*.json"))
            if comment_files:
                latest = max(comment_files, key=os.path.getctime)
                if time.time() - os.path.getctime(latest) < 120:
                    logger.info("✓ Comment generator completed successfully")
                    return
        
        if result.returncode != 0:
            raise RuntimeError("Comment generator failed - no output files created")
    
    def print_summary(self, posts_file: str):
        """Print a summary of what was generated"""
        logger.info("\n" + "="*60)
        logger.info("WORKFLOW COMPLETE")
        logger.info("="*60)
        
        # Load the posts file to get stats
        try:
            with open(posts_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            logger.info("\nPosts found:")
            logger.info(f"  Total scanned: {data.get('total_scanned', 0)}")
            logger.info(f"  Quality posts: {data.get('quality_found', 0)}")
            
            if data.get('merged_from'):
                logger.info(f"\n  Merged from {len(data['merged_from'])} runs")
            
            if data.get('post_type_distribution'):
                logger.info("\nPost types:")
                for post_type, count in sorted(
                    data['post_type_distribution'].items(), 
                    key=lambda x: x[1], 
                    reverse=True
                ):
                    logger.info(f"    {post_type}: {count}")
            
        except Exception as e:
            logger.warning(f"Could not load posts file for summary: {e}")
        
        # Find the latest comments file (profile-specific dir)
        if os.path.exists(self.comments_dir):
            comment_files = list(Path(self.comments_dir).glob("comments_*.json"))
            if comment_files:
                latest_comments = max(comment_files, key=os.path.getctime)
                
                try:
                    with open(latest_comments, 'r', encoding='utf-8') as f:
                        comments_data = json.load(f)
                    
                    logger.info("\nComments generated:")
                    logger.info(f"  Total: {comments_data.get('total', 0)}")
                    logger.info(f"  Skipped (already posted): {comments_data.get('already_posted_urls_skipped', 0)}")
                    
                    # Find the corresponding text file
                    text_file = str(latest_comments).replace('comments_', 'daily_comments_').replace('.json', '.txt')
                    if os.path.exists(text_file):
                        logger.info(f"\n📄 Ready-to-post comments: {text_file}")
                    
                except Exception as e:
                    logger.warning(f"Could not load comments file for summary: {e}")
        
        logger.info("\n✅ Workflow completed successfully!")
        logger.info("\nNext steps:")
        logger.info(f"  1. Review the comments in {self.comments_dir}/")
        logger.info("  2. Post them on LinkedIn")
        logger.info("  3. Mark as posted using mark_comments_posted.py")
    
    def run(self):
        """Run the complete workflow"""
        try:
            if self.dual_run:
                # Run finder twice and merge
                logger.info("🔄 DUAL-RUN MODE: Running post finder twice for maximum posts")
                
                # First run
                posts_file1 = self.run_post_finder(run_number=1)
                
                # Wait a bit between runs to avoid rate limiting
                logger.info("\n⏸️  Waiting 10 seconds before second run...")
                time.sleep(10)
                
                # Second run
                posts_file2 = self.run_post_finder(run_number=2)
                
                # Merge the results
                posts_file = self.merge_post_files(posts_file1, posts_file2)
                
            else:
                # Single run (original behavior)
                posts_file = self.run_post_finder()
            
            # Generate comments (unless skipped)
            if not self.skip_comments:
                self.run_comment_generator(posts_file)
            else:
                logger.info("\n⏭️  Skipping comment generation (--skip-comments)")
            
            # Print summary
            self.print_summary(posts_file)
            
            return True
            
        except KeyboardInterrupt:
            logger.warning("\n⚠️  Workflow interrupted by user")
            return False
        except Exception as e:
            logger.error(f"\n❌ Workflow failed: {e}")
            if self.debug:
                import traceback
                traceback.print_exc()
            return False


def main():
    """CLI entry point: run the full scrape → generate → post workflow."""
    parser = argparse.ArgumentParser(
        description='Run the complete LinkedIn engagement workflow',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run with defaults (uses default profile)
  python run_linkedin_workflow.py
  
  # Run with a specific profile
  python run_linkedin_workflow.py --profile work
  
  # Dual-run mode (runs finder twice and merges for more posts)
  python run_linkedin_workflow.py --dual-run
  
  # Dual-run with custom settings on a specific profile
  python run_linkedin_workflow.py --dual-run --max-posts 200 --profile personal
  
  # Debug mode
  python run_linkedin_workflow.py --debug
  
  # Just find posts, skip comment generation
  python run_linkedin_workflow.py --skip-comments
  
  # Use GPT-3.5 for cheaper comments
  python run_linkedin_workflow.py --model gpt-3.5-turbo
  
  # Manage profiles
  python linkedin_profile_manager.py list
  python linkedin_profile_manager.py add work
  python linkedin_profile_manager.py default work
        """
    )
    
    # Post finder arguments
    parser.add_argument(
        '--max-posts',
        type=int,
        default=50,
        help='Maximum posts to scan per run (default: 50)'
    )
    parser.add_argument(
        '--min-quality',
        type=int,
        default=10,
        help='Minimum quality posts to find per run (default: 10)'
    )
    
    # Comment generator arguments
    parser.add_argument(
        '--model',
        default='gpt-4o-mini',
        help='Model for comment generation (default: gpt-4)'
    )
    parser.add_argument(
        '--comment-limit',
        type=int,
        default=12,
        help='Maximum comments to generate (default: 12)'
    )
    
    # General arguments
    parser.add_argument(
        '--debug',
        action='store_true',
        help='Enable debug mode'
    )
    parser.add_argument(
        '--skip-comments',
        action='store_true',
        help='Skip comment generation, only find posts'
    )
    parser.add_argument(
        '--dual-run',
        action='store_true',
        help='Run post finder twice and merge results (gets more posts)'
    )
    parser.add_argument(
        '--profile',
        type=str,
        default=None,
        help='LinkedIn profile name to use (uses default if omitted)'
    )
    
    args = parser.parse_args()
    
    # Verify environment - check profile manager first, fall back to env vars
    from dotenv import load_dotenv
    load_dotenv()
    
    # Try to import profile manager to check for profiles
    has_profile = False
    try:
        from linkedin_automation import profile_manager as pm
        pm.auto_migrate_from_env()
        if args.profile:
            has_profile = pm.get_profile(args.profile) is not None
        else:
            has_profile = pm.get_default_profile_name() is not None
    except ImportError:
        logger.debug("linkedin_profile_manager not importable; skipping profile check", exc_info=True)

    missing_vars = []
    if not has_profile:
        if not os.getenv('LINKEDIN_USERNAME') and not os.getenv('LINKEDIN_ALT_USERNAME'):
            missing_vars.append('LINKEDIN_USERNAME (or run: python linkedin_profile_manager.py add <n>)')
        if not os.getenv('LINKEDIN_PASSWORD') and not os.getenv('LINKEDIN_ALT_PASSWORD'):
            missing_vars.append('LINKEDIN_PASSWORD (or run: python linkedin_profile_manager.py add <n>)')
    if not args.skip_comments and not os.getenv('OPENAI_API_KEY'):
        missing_vars.append('OPENAI_API_KEY')
    
    if missing_vars:
        logger.error("❌ Missing required environment variables:")
        for var in missing_vars:
            logger.error(f"   - {var}")
        logger.error("\nPlease set these in your .env file")
        sys.exit(1)
    
    # Run workflow
    profile_display = args.profile or "(default)"
    logger.info("🚀 Starting LinkedIn Engagement Workflow")
    logger.info(f"Profile: {profile_display}")
    logger.info(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
    
    runner = LinkedInWorkflowRunner(args)
    success = runner.run()
    
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()