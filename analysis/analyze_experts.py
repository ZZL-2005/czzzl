#!/usr/bin/env python3
"""分析expert能力和refine效果"""
import json
import glob
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import statistics

def load_tasks(log_dir):
    """加载所有任务数据"""
    tasks = []
    files = glob.glob(f"{log_dir}/task_*.json")

    for f in files:
        with open(f) as file:
            data = json.load(file)
            if 'scoring' in data and data['scoring']:
                tasks.append({
                    'task_id': data['task_id'],
                    'title': data['title'],
                    'category': data['category'],
                    'score': data['scoring']['score'],
                    'max_score': data['scoring']['max_score'],
                    'min_score': data['scoring'].get('min_score', 0),
                    'started_at': data.get('started_at'),
                    'completed_at': data.get('completed_at'),
                    'feedback_rounds': len(data['expert_agent']['rounds'])
                })

    # 按时间排序
    tasks.sort(key=lambda x: x['started_at'] if x['started_at'] else '')
    return tasks

def analyze_by_category(tasks):
    """按类别分析"""
    by_category = defaultdict(list)
    for task in tasks:
        by_category[task['category']].append(task)

    results = {}
    for category, cat_tasks in by_category.items():
        scores = [t['score'] for t in cat_tasks]
        results[category] = {
            'count': len(cat_tasks),
            'avg_score': statistics.mean(scores),
            'median_score': statistics.median(scores),
            'std_dev': statistics.stdev(scores) if len(scores) > 1 else 0,
            'min_score': min(scores),
            'max_score': max(scores),
            'scores': scores
        }

    return results

def analyze_time_trend(tasks):
    """按时间分三等分分析"""
    n = len(tasks)
    third = n // 3

    periods = {
        'early': tasks[:third],
        'middle': tasks[third:2*third],
        'late': tasks[2*third:]
    }

    results = {}
    for period_name, period_tasks in periods.items():
        if not period_tasks:
            continue

        scores = [t['score'] for t in period_tasks]
        start_time = period_tasks[0]['started_at']
        end_time = period_tasks[-1]['started_at']

        results[period_name] = {
            'count': len(period_tasks),
            'time_range': f"{start_time} ~ {end_time}",
            'avg_score': statistics.mean(scores),
            'median_score': statistics.median(scores),
            'std_dev': statistics.stdev(scores) if len(scores) > 1 else 0
        }

    return results

def main():
    log_dir = "/inspire/hdd/project/26summer-camp-05/26210058/czzzl/logv2"
    output_dir = "/inspire/hdd/project/26summer-camp-05/26210058/czzzl/analysis"

    print("加载任务数据...")
    tasks = load_tasks(log_dir)
    print(f"共加载 {len(tasks)} 个任务")

    print("\n分析各类别表现...")
    category_results = analyze_by_category(tasks)

    print("\n分析时间趋势...")
    time_results = analyze_time_trend(tasks)

    # 保存结果
    report = {
        'total_tasks': len(tasks),
        'by_category': category_results,
        'time_trend': time_results,
        'generated_at': datetime.now().isoformat()
    }

    output_file = f"{output_dir}/expert_analysis.json"
    with open(output_file, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    print(f"\n分析结果已保存到: {output_file}")

    # 打印摘要
    print("\n" + "="*60)
    print("分析摘要")
    print("="*60)

    print(f"\n总任务数: {len(tasks)}")

    print("\n各类别表现:")
    for cat, stats in sorted(category_results.items()):
        print(f"  {cat}:")
        print(f"    任务数: {stats['count']}")
        print(f"    平均分: {stats['avg_score']:.2f}")
        print(f"    标准差: {stats['std_dev']:.2f}")

    print("\n时间趋势分析:")
    for period, stats in [('early', time_results.get('early')),
                          ('middle', time_results.get('middle')),
                          ('late', time_results.get('late'))]:
        if stats:
            print(f"  {period.upper()}:")
            print(f"    时间段: {stats['time_range']}")
            print(f"    任务数: {stats['count']}")
            print(f"    平均分: {stats['avg_score']:.2f}")

if __name__ == '__main__':
    main()
